"""Command text → QueryAST, plus the QueryAST → engine-payload mapping.

A **pure function of the document**: undo/redo of the doc restores AST states
exactly because the AST is always recomputed from text. Ported 1:1 from the
earlier TypeScript implementation (regex-for-regex, message-for-message, and the
AST -> payload mapping alike); the contract is `docs/AST.md`, pinned by the
`spec/canonical-query.anyql` / `spec/canonical-query.ast.json` fixture pair.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .alias import effective_alias
from .ast import (
    AggCall,
    CaseBranch,
    CaseClause,
    FromClause,
    GroupTerm,
    JoinClause,
    OrderTerm,
    QueryAST,
    QueryError,
    RankCall,
    SelectItem,
    TemporalCall,
    WhereClause,
    WindowFrame,
    WindowOrder,
    WithClause,
)
from .schema import AGGREGATES, TEMPORAL, open_tables_of, schema_tables, table_by_name

# A column reference: bare (`amount`) or qualified with one dot (`users.score`).
_COL = r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?"
_COL_RE = re.compile(rf"^{_COL}$")
_AGG_RE = re.compile(rf"^([A-Za-z_][A-Za-z0-9_]*)\((\*|{_COL})\)$")
_AS_RE = re.compile(r"^(.*?)\s+as\s+([A-Za-z_][A-Za-z0-9_]*)$", re.IGNORECASE)
_WHERE_RE = re.compile(rf"^({_COL})\s*(=|!=|>=|<=|>|<|like)\s*(.+)$", re.IGNORECASE)
_ORDER_RE = re.compile(rf"^({_COL})(?:\s+(asc|desc))?$", re.IGNORECASE)
# `\join <dataset> [as] <alias> on <left>[ = <right>]` — a bare identifier
# between the dataset and `on` is the alias (`as` optional, `on` delimits).
_JOIN_RE = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_]*)"  # 1: dataset
    r"(?:\s+(?:as\s+)?([A-Za-z_][A-Za-z0-9_]*))?"  # 2: optional [as] alias
    r"\s+on\s+"
    r"([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)"  # 3: left (identifier-qualified)
    r"(?:\s*=\s*([A-Za-z_][A-Za-z0-9_]*))?$",  # 4: optional right
    re.IGNORECASE,
)
# `\from`/`\open <dataset> [as] <alias>` — a bare second identifier is the alias.
_FROM_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(?:\s+(?:as\s+)?([A-Za-z_][A-Za-z0-9_]*))?$", re.IGNORECASE)
_CMD_RE = re.compile(r"^\\([A-Za-z_][A-Za-z0-9_]*)\s*(.*)$")
# `\case <alias> = when …` — the tail is scanned for when/then/else segments.
_CASE_HEAD_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
# `\select <expr> over ( [partition by <cols>] [order by <col> [asc|desc]] )`
# — the frame is the LAST parenthesized tail of a select expression.
_OVER_TAIL_RE = re.compile(r"^\s*(.*?)\s+over\s*\(\s*([^()]*)\s*\)\s*$", re.IGNORECASE)
# Rank functions take no argument; they only exist as `<fn>() over ( … )`.
_RANK_RE = re.compile(r"^(rank|dense_rank|row_number)\(\s*\)$", re.IGNORECASE)
_RANK_NAME_RE = re.compile(r"^(rank|dense_rank|row_number)$", re.IGNORECASE)
# `over ( [partition by <cols>] [order by <col> [asc|desc]] )` — at least one.
_PARTITION_RE = re.compile(r"^\s*partition\s+by\s+([\w.,\s]+?)\s*$", re.IGNORECASE | re.ASCII)
_ORDER_TAIL_RE = re.compile(rf"^\s*order\s+by\s+({_COL})(?:\s+(asc|desc))?\s*$", re.IGNORECASE)
# `\with <name>` CTE header — the name must not be empty.
_WITH_NAME_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)$", re.IGNORECASE)
# `/^\d+$/` in TS is ASCII-only; Python's `\d` is not.
_LIMIT_RE = re.compile(r"^[0-9]+$")
_WHEN_HEAD_RE = re.compile(r"^when\b", re.IGNORECASE | re.ASCII)
_INDENT_RE = re.compile(r"^\s", re.ASCII)

NEEDS_ARGS = frozenset(
    {"from", "open", "join", "select", "where", "group", "order", "limit", "case", "with"}
)

_PAREN_SPLIT_RE = re.compile(r"[,\s]+")


def split_top(text: str) -> list[str]:
    """Split on commas at paren depth 0: `\\select a, sum(b) as x, c` → 3 segments."""
    out: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(text):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            out.append(text[start:i].strip())
            start = i + 1
    out.append(text[start:].strip())
    # Empty segments (leading/trailing commas) are simply not expressions.
    return [s for s in out if s]


def unquote(raw: str) -> str:
    """Strip one layer of surrounding quotes — "anything with a space is a wrapped string"."""
    text = raw.strip()
    for quote in ('"', "'"):
        if len(text) >= 2 and text.startswith(quote) and text.endswith(quote):
            return text[1:-1]
    return text


def keyword_positions(text: str, keyword: str) -> list[int]:
    """Positions of a keyword at word boundaries outside quotes/parens.

    So a quoted value containing the word never splits a clause.
    """
    out: list[int] = []
    word = re.compile(r"\b" + re.escape(keyword) + r"\b", re.IGNORECASE | re.ASCII)
    quote: str | None = None
    depth = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if quote is not None:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        else:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if depth == 0:
                match = word.match(text, i)
                if match is not None:
                    out.append(i)
                    i = match.end()
                    continue
        i += 1
    return out


def parse_case_arg(rest: str) -> CaseClause | None:
    """Parse a `\\case` argument: `<alias> = when <cond> then <val> [when …] [else <val>]`.

    Returns `None` when the text doesn't parse (half-typed lines included).
    """
    head = _CASE_HEAD_RE.match(rest)
    if head is None:
        return None
    alias = head.group(1)
    tail = head.group(2).strip()
    if not _WHEN_HEAD_RE.match(tail):
        return None

    whens: list[CaseBranch] = []
    else_value: str | None = None

    else_positions = keyword_positions(tail, "else")
    else_at = else_positions[-1] if else_positions else -1
    if else_at >= 0:
        else_raw = tail[else_at + 4 :].strip()
        if not else_raw:
            return None
        else_value = unquote(else_raw)
        tail = tail[:else_at].strip()

    when_positions = keyword_positions(tail, "when")
    if not when_positions:
        return None
    for i, when_at in enumerate(when_positions):
        seg_start = when_at + 4
        seg_end = when_positions[i + 1] if i + 1 < len(when_positions) else len(tail)
        seg = tail[seg_start:seg_end].strip()
        then_positions = keyword_positions(seg, "then")
        if len(then_positions) != 1:
            return None
        cond = seg[: then_positions[0]].strip()
        then_val = seg[then_positions[0] + 4 :].strip()
        if not then_val:
            return None
        cond_match = _WHERE_RE.match(cond)
        if cond_match is None:
            return None
        whens.append(
            CaseBranch(
                column=cond_match.group(1),
                op=cond_match.group(2).lower(),
                value=unquote(cond_match.group(3)),
                then=unquote(then_val),
            )
        )
    if not whens:
        return None
    return CaseClause(line=0, raw=rest, alias=alias, whens=whens, else_=else_value)


def parse_over_frame(tail_raw: str) -> WindowFrame | None:
    """Parse the inside of `over ( … )`: `[partition by <col>…] [order by <col> [asc|desc]]`.

    At least one of the two. Returns `None` when it doesn't parse.
    """
    tail = tail_raw.strip()
    if not tail:
        return None

    partition_by: list[str] = []
    order: WindowOrder | None = None
    work = tail

    order_at = keyword_positions(work, "order")
    order_text = ""
    if order_at:
        if len(order_at) > 1:
            return None
        order_text = work[order_at[0] :]
        work = work[: order_at[0]].strip()

    if work:
        part = _PARTITION_RE.match(work)
        if part is None:
            return None
        partition_by = [c for c in _PAREN_SPLIT_RE.split(part.group(1)) if c]
        if not partition_by or not all(_COL_RE.match(c) for c in partition_by):
            return None

    if order_text:
        order_match = _ORDER_TAIL_RE.match(order_text)
        if order_match is None:
            return None
        direction = order_match.group(2)
        order = WindowOrder(
            column=order_match.group(1),
            direction=direction.lower() if direction else "asc",
        )

    if not partition_by and order is None:
        return None
    return WindowFrame(partition_by=partition_by, order=order)


def block_extent(lines: list[str], header: int) -> int:
    """The index one past the last line of the `\\with` block at `header`.

    The body is every indented line under the header, and blank lines only
    while the next non-blank line is indented. One implementation, shared by the
    parser's `\\with` scan and by `clause_line`, so no caller invents a second.
    """
    j = header + 1
    while j < len(lines):
        if not lines[j].strip():
            # A blank line stays part of the block only when it continues.
            continues = False
            for k in range(j + 1, len(lines)):
                if not lines[k].strip():
                    continue
                continues = bool(_INDENT_RE.match(lines[k]))
                break
            if not continues:
                break
            j += 1
            continue
        if not _INDENT_RE.match(lines[j]):
            break
        j += 1
    return j


def block_bounds(lines: list[str], line: int) -> tuple[int, int]:
    """The `[start, end)` line indexes of the block that owns a 1-based line.

    A block is the whole document or one `\\with` body; the innermost body wins,
    which is the one the caret is editing.
    """
    start, end = 0, len(lines)
    while True:
        for index in range(start, end):
            match = _CMD_RE.match(lines[index].strip())
            if match is None or match.group(1).lower() != "with":
                continue
            stop = block_extent(lines, index)
            if index + 1 < line <= stop:
                start, end = index + 1, stop
                break
        else:
            return start, end


def clause_line(doc: str, line: int, command: str) -> int | None:
    """The document line carrying that clause in the block that owns `line`.

    `\\from` and `\\open` are the same clause, and a repeated clause answers
    with its LAST line — the one a new item extends. `None` means this block
    does not carry the clause yet. The palette's clause commands use this so
    that asking for a clause never writes a second one beside the first.
    """
    lines = doc.split("\n")
    line = max(1, min(line, len(lines)))
    start, end = block_bounds(lines, line)
    wanted = "from" if command.lower() == "open" else command.lower()
    found: int | None = None
    index = start
    while index < end:
        match = _CMD_RE.match(lines[index].strip())
        if match is None:
            index += 1
            continue
        name = match.group(1).lower()
        if name == "with":
            # The header is this block's clause; its body is a block of its own.
            if wanted == "with":
                found = index + 1
            index = block_extent(lines, index)
            continue
        if ("from" if name == "open" else name) == wanted:
            found = index + 1
        index += 1
    return found


@dataclass
class ParseOpts:
    # Line-number offset: body lines report their absolute document line.
    offset: int = 0
    # Absolute 1-based line being typed (errors suppressed there); 0 = none.
    typing_line: int = 0
    # CTE names visible to this slice (defined before it, in document order).
    visible_ctes: list[str] = field(default_factory=list)
    # False inside a `\with` body: nested CTEs and a nested `\with` are errors.
    top: bool = True


def parse_slice(lines: list[str], opts: ParseOpts) -> QueryAST:
    """Parse one slice of command lines into a QueryAST-shaped result."""
    ast = QueryAST()

    def visible_ctes() -> list[str]:
        # CTEs already defined in this slice (document order) plus any inherited
        # from an enclosing slice — the names a `\from`/`\join` here may reference.
        return [*opts.visible_ctes, *[c.name for c in ast.with_]]

    # Errors are only reported for settled lines; `typing_line` is the last
    # non-empty line of the WHOLE document (the one being typed), where
    # half-finished commands are normal — a `\with` body slice mid-document
    # must still report its own errors.
    i = 0
    while i < len(lines):
        line = i + 1 + opts.offset
        text = lines[i].strip()
        if not text:
            i += 1
            continue
        typing = line == opts.typing_line

        def err(message: str, _line: int = line, _typing: bool = typing) -> None:
            if not _typing:
                ast.errors.append(QueryError(line=_line, message=message))

        if not text.startswith("\\"):
            err("not a command — lines start with \\")
            i += 1
            continue
        m = _CMD_RE.match(text)
        if m is None:
            err("incomplete command")
            i += 1
            continue

        cmd = m.group(1).lower()
        rest = m.group(2).strip()

        if not rest and cmd in NEEDS_ARGS:
            err(f"\\{cmd} expects arguments")
            i += 1
            continue

        if cmd == "with":
            w = _WITH_NAME_RE.match(rest)
            if w is None:
                err("\\with expects a bare CTE name")
                i += 1
                continue
            if not opts.top:
                err("nested CTEs are not supported")
                i += 1
                continue
            name = w.group(1)
            if any(c.name == name for c in ast.with_):
                err(f'duplicate CTE name "{name}"')
                i += 1
                continue
            body_start = i + 1
            j = block_extent(lines, i)
            body: list[str] = lines[body_start:j]
            i = j
            if not any(b.strip() for b in body):
                err(f"\\with {name} expects an indented body")
                continue
            body_ast = parse_slice(
                body,
                ParseOpts(
                    offset=body_start + opts.offset,
                    typing_line=opts.typing_line,
                    visible_ctes=visible_ctes(),
                    top=False,
                ),
            )
            # Body errors belong to the document; nested CTE errors too.
            ast.errors.extend(body_ast.errors)
            body_ast.errors = []
            ast.with_.append(WithClause(line=line, name=name, body=body_ast))
            if schema_tables() and name not in opts.visible_ctes and table_by_name(name):
                # The CTE name shadows nothing, that's fine; only a dataset
                # collision is worth flagging (the dataset becomes unreachable).
                err(f'CTE name "{name}" shadows dataset "{name}"')
            continue

        if cmd in ("from", "open"):
            f = _FROM_RE.match(rest)
            if f is None:
                err(f"\\{cmd} expects a dataset name")
                i += 1
                continue
            ast.from_ = FromClause(line=line, table=f.group(1), alias=f.group(2) or None)
            # Only complain about unknown tables once the schema has loaded —
            # while the backend is offline the registry is legitimately empty.
            # CTE names defined earlier in the document count as known tables.
            if (
                schema_tables()
                and not table_by_name(f.group(1))
                and f.group(1) not in visible_ctes()
            ):
                err(
                    f'unknown table "{f.group(1)}" — loaded datasets: '
                    + ", ".join(t.name for t in schema_tables())
                )
            i += 1
            continue

        if cmd == "join":
            jm = _JOIN_RE.match(rest)
            if jm is None:
                err("\\join expects `<dataset> [as] <alias> on <col>[ = <col>]`")
                i += 1
                continue
            ast.joins.append(
                JoinClause(
                    line=line,
                    dataset=jm.group(1),
                    alias=jm.group(2) or None,
                    left=jm.group(3),
                    right=jm.group(4) or jm.group(3),
                )
            )
            # Like `\from`: only complain once the schema has loaded.
            if (
                schema_tables()
                and not table_by_name(jm.group(1))
                and jm.group(1) not in visible_ctes()
            ):
                err(
                    f'unknown table "{jm.group(1)}" — loaded datasets: '
                    + ", ".join(t.name for t in schema_tables())
                )
            i += 1
            continue

        if cmd == "select":
            # One `\select` line may carry several comma-separated expressions;
            # each segment parses exactly like a single-expression line. A
            # trailing `over ( … )` attaches a window frame to the expression.
            for expr in split_top(rest):
                item = SelectItem(line=line, raw=expr)
                core = expr
                as_match = _AS_RE.match(expr)
                if as_match:
                    core = as_match.group(1).strip()
                    item.alias = as_match.group(2)
                over = _OVER_TAIL_RE.match(core)
                if over:
                    frame = parse_over_frame(over.group(2))
                    if frame is None:
                        err("over (...) needs partition by <cols> and/or order by <col> [asc|desc]")
                        ast.select.append(item)
                        continue
                    item.window = frame
                    core = over.group(1).strip()
                # `\select *` — a whole star expansion, projectable only on its
                # own (no alias, no frame). Qualified `t.*` is not supported.
                if core == "*":
                    item.star = True
                    if item.alias:
                        err("`*` cannot take an alias")
                    if item.window:
                        err("`*` cannot carry over (...)")
                    ast.select.append(item)
                    continue
                rank = _RANK_RE.match(core)
                if rank:
                    fn = rank.group(1).lower()
                    item.rank = RankCall(fn=fn)  # type: ignore[arg-type]
                    if not item.window:
                        err(f"{fn}() requires over ( ... )")
                    elif not item.window.order:
                        err(f"{fn}() requires order by inside over (...)")
                else:
                    agg = _AGG_RE.match(core)
                    if agg:
                        fn = agg.group(1).lower()
                        if fn in AGGREGATES:
                            item.aggregate = AggCall(fn=fn, arg=agg.group(2))  # type: ignore[arg-type]
                        elif fn in TEMPORAL:
                            # Parsed structurally like an aggregate; the dtype
                            # compatibility check is the engine's (the parser
                            # stays pure per dtype availability).
                            if item.window:
                                err(f'"{fn}" is not a window function — use sum/avg/count/min/max')
                            item.temporal = TemporalCall(fn=fn, arg=agg.group(2))  # type: ignore[arg-type]
                        elif _RANK_NAME_RE.match(fn):
                            err(f'"{fn}" takes no arguments — write {fn}() over ( ... )')
                        else:
                            err(f'unknown function "{agg.group(1)}"')
                    elif _COL_RE.match(core):
                        if item.window:
                            err("a plain column cannot carry over (...) — wrap it in a function")
                        item.column = core
                    else:
                        err(f'cannot parse expression "{core}"')
                ast.select.append(item)
            i += 1
            continue

        if cmd == "where":
            wm = _WHERE_RE.match(rest)
            if wm is None:
                err("\\where expects `column op value`")
                i += 1
                continue
            ast.where = WhereClause(
                line=line,
                raw=rest,
                column=wm.group(1),
                op=wm.group(2).lower(),
                value=unquote(wm.group(3)),
            )
            i += 1
            continue

        if cmd == "group":
            parts = [p for p in _PAREN_SPLIT_RE.split(rest) if p]
            if not parts:
                err("\\group expects a column")
                i += 1
                continue
            for p in parts:
                if not _COL_RE.match(p):
                    err(f'\\group: not a column: "{p}"')
                    continue
                ast.group_by.append(GroupTerm(line=line, column=p))
            i += 1
            continue

        if cmd == "order":
            om = _ORDER_RE.match(rest)
            if om is None:
                err("\\order expects `target [asc|desc]`")
                i += 1
                continue
            direction = om.group(2)
            ast.order_by.append(
                OrderTerm(
                    line=line,
                    target=om.group(1),
                    direction=direction.lower() if direction else "asc",  # type: ignore[arg-type]
                    resolves_to=None,
                )
            )
            i += 1
            continue

        if cmd == "case":
            case = parse_case_arg(rest)
            if case is None:
                err('\\case expects `<alias> = when <col> <op> <value> then <value> [when …] [else <value>]`')
                i += 1
                continue
            case.line = line
            ast.cases.append(case)
            i += 1
            continue

        if cmd == "limit":
            if not _LIMIT_RE.match(rest):
                err("limit must be a non-negative integer")
                i += 1
                continue
            ast.limit = int(rest)
            i += 1
            continue

        err(f'unknown command "\\{cmd}"')
        i += 1

    validate(ast, opts)
    return ast


def validate(ast: QueryAST, opts: ParseOpts) -> None:
    """Identifier/column validation shared by the document and every `\\with` body."""
    # Each open table is addressed by its identifier: the alias when set,
    # otherwise the dataset name. Identifiers must be unique — aliasing is
    # strict, so a duplicate would make one table unreachable.
    clauses: list[tuple[int, str]] = []
    if ast.from_ is not None:
        clauses.append((ast.from_.line, ast.from_.alias or ast.from_.table))
    for j in ast.joins:
        clauses.append((j.line, j.alias or j.dataset))
    seen: set[str] = set()
    for clause_line, identifier in clauses:
        if identifier in seen:
            if clause_line != opts.typing_line:
                ast.errors.append(
                    QueryError(
                        line=clause_line,
                        message=f'duplicate table identifier "{identifier}"',
                    )
                )
            break
        seen.add(identifier)

    # Qualified column refs (`users.score`, `e.user_id`) name their table by
    # IDENTIFIER: an aliased table answers only to its alias, so the dataset
    # name of an aliased table behaves as an unknown column. Only validated
    # once the schema loaded (mirrors `\from`), and never on the typed line.
    if schema_tables():
        open_idents = [t.identifier for t in open_tables_of(ast.from_, ast.joins)]

        def check(ref_line: int, ref: str) -> None:
            if ref_line == opts.typing_line or "." not in ref:
                return
            prefix = ref[: ref.index(".")]
            if prefix not in open_idents:
                listed = ", ".join(open_idents) or "(no \\from)"
                ast.errors.append(
                    QueryError(
                        line=ref_line,
                        message=f'unknown column "{ref}" — "{prefix}" is not an open table: {listed}',
                    )
                )

        for s in ast.select:
            check(s.line, s.column or "")
            if s.aggregate:
                check(s.line, s.aggregate.arg)
            if s.temporal:
                check(s.line, s.temporal.arg)
            if s.window:
                for p in s.window.partition_by:
                    check(s.line, p)
                if s.window.order:
                    check(s.line, s.window.order.column)
        if ast.where:
            check(ast.where.line, ast.where.column)
        for g in ast.group_by:
            check(g.line, g.column)
        for o in ast.order_by:
            check(o.line, o.target)
        for c in ast.cases:
            for w in c.whens:
                check(c.line, w.column)
        # A qualified `\join` left side names the accumulated left: identifiers
        # established BEFORE this clause (the clause's own alias doesn't count).
        sofar: list[str] = [ast.from_.alias or ast.from_.table] if ast.from_ else []
        for j in ast.joins:
            if "." in j.left:
                prefix = j.left[: j.left.index(".")]
                if j.line != opts.typing_line and prefix not in sofar:
                    listed = ", ".join(sofar) or "(no \\from)"
                    ast.errors.append(
                        QueryError(
                            line=j.line,
                            message=f'unknown column "{j.left}" — "{prefix}" is not an open table: {listed}',
                        )
                    )
            sofar = [*sofar, j.alias or j.dataset]

    # Resolve order targets against the full select list (1-based, as
    # displayed). Alias-first, and aggregate auto-aliases count as real output
    # names (`sum(amount)` answers to `amount_sum` in `\order`). Case and window
    # columns carry required aliases and resolve like select aliases.
    for term in ast.order_by:
        idx = next(
            (
                i
                for i, s in enumerate(ast.select)
                if effective_alias(s) == term.target or s.column == term.target
            ),
            -1,
        )
        if idx >= 0:
            term.resolves_to = idx + 1
        elif any(c.alias == term.target for c in ast.cases):
            term.resolves_to = None  # a real output column, not a select item


def parse_query(doc: str) -> QueryAST:
    """Parse a whole document into its QueryAST (pure function of `doc`)."""
    lines = doc.split("\n")
    last_content_line = 0
    for i, raw in enumerate(lines):
        if raw.strip():
            last_content_line = i + 1
    return parse_slice(lines, ParseOpts(offset=0, typing_line=last_content_line, visible_ctes=[], top=True))


def payload_from_ast(ast: QueryAST) -> dict:
    """QueryAST → the engine payload, 1:1 with the wire contract.

    An empty select list means "all columns", `\\join` clauses pass through in
    document order (always a list, possibly empty), `\\order` targets pass
    through verbatim (the engine resolves aliases first, then raw columns),
    `\\limit` maps to `limit`. Column strings may be qualified (`users.score`,
    `e.user_id` — identifiers, not necessarily dataset names) and reach the
    engine untouched. Table aliases ride along (`alias`, always present, `None`
    when unaliased). Aggregate select items carry the derived auto-alias
    (`auto_alias`) when the user gave none — the engine re-derives the same
    name for null aliases; sending it up front keeps the two layers identical.
    """
    return {
        "dataset": ast.from_.table if ast.from_ else "",
        "alias": ast.from_.alias if ast.from_ else None,
        "joins": [
            {"dataset": j.dataset, "alias": j.alias, "left": j.left, "right": j.right}
            for j in ast.joins
        ],
        "select": [
            {
                "column": s.column,
                "star": s.star,
                "aggregate": {"fn": s.aggregate.fn, "arg": s.aggregate.arg} if s.aggregate else None,
                "temporal": {"fn": s.temporal.fn, "arg": s.temporal.arg} if s.temporal else None,
                "rank": {"fn": s.rank.fn} if s.rank else None,
                "window": (
                    {
                        "partitionBy": list(s.window.partition_by),
                        "order": (
                            {"column": s.window.order.column, "direction": s.window.order.direction}
                            if s.window.order
                            else None
                        ),
                    }
                    if s.window
                    else None
                ),
                "alias": effective_alias(s),
            }
            for s in ast.select
        ],
        "where": (
            {"column": ast.where.column, "op": ast.where.op, "value": ast.where.value}
            if ast.where
            else None
        ),
        "groupBy": [g.column for g in ast.group_by],
        "orderBy": [{"target": o.target, "direction": o.direction} for o in ast.order_by],
        "limit": ast.limit,
        "cases": [
            {
                "alias": c.alias,
                "whens": [
                    {"column": w.column, "op": w.op, "value": w.value, "then": w.then} for w in c.whens
                ],
                "else": c.else_,
            }
            for c in ast.cases
        ],
        # A CTE body is a full payload minus its own (empty, by grammar) `with` list.
        "ctes": [{"name": c.name, "body": payload_from_ast(c.body)} for c in ast.with_],
    }
