"""Command text → QueryAST, plus the QueryAST → engine-payload mapping.

A **pure function of the document**: undo/redo of the doc restores AST states
exactly because the AST is always recomputed from text. Ported 1:1 from the
earlier TypeScript implementation (regex-for-regex, message-for-message, and the
AST -> payload mapping alike); the contract is `docs/AST.md`, pinned by the
`spec/canonical-query.anyql` / `spec/canonical-query.ast.json` fixture pair.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from collections.abc import Sequence

from .alias import effective_alias
from .ast import (
    AggCall,
    CaseBranch,
    CaseClause,
    ColumnRef,
    DropClause,
    FrameBounds,
    FromClause,
    GroupTerm,
    JoinClause,
    LiteralValue,
    OrderTerm,
    QueryAST,
    QueryError,
    RankCall,
    RegexCall,
    ScalarCall,
    SelectItem,
    SetOpClause,
    TempClause,
    TemporalCall,
    TxDirective,
    WhereClause,
    WindowFrame,
    WindowOrder,
    WithClause,
)
from .functions import SCALAR_FUNCTIONS
from .schema import (
    AGGREGATES,
    TEMPORAL,
    open_tables_of,
    EMPTY_SCHEMA,
    SchemaContext,
)

# A column reference: bare (`amount`) or qualified with one dot (`users.score`).
_COL = r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?"
_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_COL_RE = re.compile(rf"^{_COL}$")
_AGG_RE = re.compile(rf"^([A-Za-z_][A-Za-z0-9_]*)\((\*|{_COL})\)$")
_CALL_HEAD_RE = re.compile(rf"^({_IDENT})\s*\(")
_AS_RE = re.compile(r"^(.*?)\s+as\s+([A-Za-z_][A-Za-z0-9_]*)$", re.IGNORECASE)
# `\where <column> <op> <tail>`; the tail is a value, or `( … )` for `in`/`not in`
# and for the scalar-subquery form of a comparison.
_WHERE_HEAD_RE = re.compile(
    rf"^({_COL})\s+(not\s+in|in|!=|!~|>=|<=|like|~|=|>|<)\s*(.*)$", re.IGNORECASE
)
# The `\where` operators that take an inline subquery instead of a literal.
SUBQUERY_OPS = frozenset({"in", "not in"})
# The regex operators: POSIX-style matching, SQL's `~`/`!~`.
REGEX_OPS = frozenset({"~", "!~"})
_ORDER_RE = re.compile(rf"^({_COL})(?:\s+(asc|desc))?$", re.IGNORECASE)
# The `on <col>[ = <col>]` tail of a `\join`, on its own (a lateral join's `on`
# is optional, so the tail is matched separately from the source). Either side
# may be identifier-qualified — the left usually is.
_JOIN_ON_RE = re.compile(rf"^on\s+({_COL})(?:\s*=\s*({_COL}))?$", re.IGNORECASE)
# `\union`/`\intersect`/`\except [all|distinct] <dataset>` — the modifier comes
# first, and absent means SQL's default (`distinct`): rows are deduplicated.
_SET_OP_RE = re.compile(rf"^(?:(all|distinct)\s+)?({_IDENT})$", re.IGNORECASE)
_CMD_RE = re.compile(r"^\\([A-Za-z_][A-Za-z0-9_]*)\s*(.*)$")
# `\case <alias> = when …` — the tail is scanned for when/then/else segments.
_CASE_HEAD_RE = re.compile(rf"^({_IDENT})\s*=\s*(.*)$")
# `\select <expr> over ( [partition by <cols>] [order by <col> [asc|desc]] )`
# — the frame is the LAST parenthesized tail of a select expression.
_OVER_TAIL_RE = re.compile(r"^\s*(.*?)\s+over\s*\(\s*([^()]*)\s*\)\s*$", re.IGNORECASE)
# Rank functions take no argument; they only exist as `<fn>() over ( … )`.
_RANK_RE = re.compile(r"^(rank|dense_rank|row_number)\(\s*\)$", re.IGNORECASE)
_RANK_NAME_RE = re.compile(r"^(rank|dense_rank|row_number)$", re.IGNORECASE)
# `over ( [partition by <cols>] [order by <col> [asc|desc]] )` — at least one.
_PARTITION_START_RE = re.compile(r"partition\s+by\s+", re.IGNORECASE | re.ASCII)
_ORDER_START_RE = re.compile(r"order\s+by\b", re.IGNORECASE | re.ASCII)
_WINDOW_COLUMN_RE = re.compile(_COL)
_WINDOW_ORDER_RE = re.compile(
    rf"order\s+by\s+({_COL})(?:\s+(asc|desc))?(?=\s|$)", re.IGNORECASE | re.ASCII
)
_FRAME_START_RE = re.compile(r"(?<![\w.])(?:rows|range)\s+between\b", re.IGNORECASE | re.ASCII)
# `rows|range between <bound> and <bound>` — the window frame inside `over (...)`.
_FRAME_HEAD_RE = re.compile(r"^(rows|range)\s+between\s+(.+?)\s+and\s+(.+)$", re.IGNORECASE | re.ASCII)
_FRAME_BOUND_RE = re.compile(
    r"^(?:unbounded\s+preceding|([0-9]+)\s+preceding|current\s+row|([0-9]+)\s+following|unbounded\s+following)$",
    re.IGNORECASE | re.ASCII,
)
# `regexp_extract(<col>, <pattern>[, <group>])` / `regexp_replace(<col>, <pattern>, <replacement>)`.
_REGEX_FN_RE = re.compile(r"^(regexp_extract|regexp_replace)\s*\((.*)\)$", re.IGNORECASE | re.ASCII)
# `\with <name>` / `\temp <name>` / `\drop <name>` — a bare identifier argument.
_WITH_NAME_RE = re.compile(rf"^({_IDENT})$", re.IGNORECASE)
# `/^\d+$/` in TS is ASCII-only; Python's `\d` is not.
_LIMIT_RE = re.compile(r"^[0-9]+$")
_NUMBER_RE = re.compile(r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$")
_STRING_RE = re.compile(r'''(?:'(?:[^']|'')*'|"(?:[^"]|"")*")''')
_WHEN_HEAD_RE = re.compile(r"^when\b", re.IGNORECASE | re.ASCII)
_INDENT_RE = re.compile(r"^\s", re.ASCII)

NEEDS_ARGS = frozenset(
    {
        "from",
        "open",
        "join",
        "union",
        "intersect",
        "except",
        "select",
        "where",
        "group",
        "order",
        "limit",
        "case",
        "with",
        "temp",
        "drop",
        "savepoint",
        "release",
    }
)

# The set-operation commands. Each takes `[all|distinct] <dataset|( … )>`.
SET_OP_COMMANDS = frozenset({"union", "intersect", "except"})
# Alternate command spellings share one clause identity in the editor.
COMMAND_ALIASES = {"open": "from", "unique": "distinct"}
# `\union all` on its own names nothing: a bare modifier is half a clause.
SET_OP_MODIFIERS = frozenset({"all", "distinct"})

# The transaction commands and the kind each maps to. `\rollback` and
# `\rollback to <name>` are one command with two kinds.
TX_COMMANDS: dict[str, str] = {
    "begin": "begin",
    "commit": "commit",
    "rollback": "rollback",
    "savepoint": "savepoint",
    "release": "release",
}
# The ones that take no argument at all.
TX_BARE = frozenset({"begin", "commit"})

_PAREN_SPLIT_RE = re.compile(r"[,\s]+")

# At CPython's minimum configurable integer-string limit (640 digits).
MAX_NUMERIC_CHARS = 640
MAX_FUNCTION_DEPTH = 16
MAX_FUNCTION_EXPANSIONS = 256


def checked_integer(text: str, label: str) -> int:
    if len(text) > MAX_NUMERIC_CHARS:
        raise ValueError(f"{label} is too large (maximum {MAX_NUMERIC_CHARS} characters)")
    try:
        return int(text)
    except ValueError:
        raise ValueError(f"{label} must be an integer") from None


def split_top(text: str, *, keep_empty: bool = False) -> list[str]:
    """Split on commas at paren depth 0: `\\select a, sum(b) as x, c` → 3 segments.

    Quoted strings are opaque — a comma inside one (`regexp_extract(p, "a,b")`)
    is part of the value, not a separator.
    """
    out: list[str] = []
    depth = 0
    quote: str | None = None
    start = 0
    for i, ch in enumerate(text):
        if quote is not None:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            out.append(text[start:i].strip())
            start = i + 1
    out.append(text[start:].strip())
    # Select lists tolerate empty segments; function arguments must preserve them.
    return out if keep_empty else [s for s in out if s]


def unquote(raw: str) -> str:
    """Strip one layer of surrounding quotes — "anything with a space is a wrapped string"."""
    text = raw.strip()
    for quote in ('"', "'"):
        if len(text) >= 2 and text.startswith(quote) and text.endswith(quote):
            return text[1:-1]
    return text


def parse_literal(text: str) -> LiteralValue | None:
    """Read a scalar constant without coercing quoted numeric text."""
    if _STRING_RE.fullmatch(text):
        quote = text[0]
        return LiteralValue(text[1:-1].replace(quote * 2, quote))
    if _NUMBER_RE.fullmatch(text):
        if not any(char in text.lower() for char in ".e"):
            return LiteralValue(checked_integer(text, "numeric literal"))
        if len(text) > MAX_NUMERIC_CHARS:
            raise ValueError(f"numeric literal is too large (maximum {MAX_NUMERIC_CHARS} characters)")
        value = float(text)
        return LiteralValue(value) if math.isfinite(value) else None
    keyword = text.lower()
    if keyword in {"true", "false", "null"}:
        return LiteralValue(None if keyword == "null" else keyword == "true")
    return None


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


def take_paren(text: str) -> tuple[str, str] | None:
    """Split `( … ) rest` into the balanced body and what follows it.

    Quote- and depth-aware, so a nested `( … )` or a quoted paren stays inside
    the body. `None` when the text does not start a balanced group — a
    half-typed line is normal, not an error to report.
    """
    s = text.strip()
    if not s.startswith("("):
        return None
    depth = 0
    quote: str | None = None
    for i, ch in enumerate(s):
        if quote is not None:
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return s[1:i], s[i + 1 :].strip()
    return None


def parse_scalar_call(text: str) -> ScalarCall:
    """Read only catalog calls; nesting never admits aggregates or subqueries."""
    head = _CALL_HEAD_RE.match(text)
    if head is None:
        raise ValueError(f'cannot parse scalar expression "{text}"')
    fn = head.group(1).lower()
    signature = SCALAR_FUNCTIONS.get(fn)
    if signature is None:
        raise ValueError(f'unknown scalar function "{head.group(1)}"')
    paren = take_paren(text[head.end() - 1 :])
    if paren is None or paren[1]:
        raise ValueError(f'{fn}() needs balanced parentheses with no trailing expression')
    parts = split_top(paren[0], keep_empty=True) if paren[0].strip() else []
    if any(not part for part in parts):
        raise ValueError(f'{fn}() cannot contain an empty argument')
    if not signature.accepts(len(parts)):
        maximum = len(signature.parameters)
        count = (
            f"at least {signature.minimum}"
            if signature.variadic
            else str(maximum) if signature.minimum == maximum
            else f"{signature.minimum} to {maximum}"
        )
        raise ValueError(f'{fn}() takes {count} argument(s) — got {len(parts)}')
    args: list[ColumnRef | LiteralValue | ScalarCall] = []
    for part in parts:
        literal = parse_literal(part)
        if literal is not None:
            args.append(literal)
        elif _COL_RE.fullmatch(part):
            args.append(ColumnRef(part))
        else:
            args.append(parse_scalar_call(part))
    return ScalarCall(fn=fn, args=args)


def split_commands(body: str) -> list[str]:
    """An inline `( … )` subquery body as command lines.

    The body is the same `\\command` grammar on one document line, so it is cut
    at every `\\` that sits at depth 0 outside quotes — a `\\` inside a nested
    group or a quoted value belongs to that value. Each piece keeps its `\\`,
    so the identical `parse_slice` reads it.
    """
    out: list[str] = []
    depth = 0
    quote: str | None = None
    start = -1
    for i, ch in enumerate(body):
        if quote is not None:
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            continue
        if ch == "(":
            depth += 1
            continue
        if ch == ")":
            depth -= 1
            continue
        if ch == "\\" and depth == 0:
            if start >= 0:
                out.append(body[start:i].strip())
            start = i
    if start >= 0:
        out.append(body[start:].strip())
    return [s for s in out if s]


def parse_subquery(body: str, line: int, opts: ParseOpts, outer: list[str] | None = None) -> QueryAST:
    """Parse an inline `( … )` body as a query of its own, on the clause's line.

    The same grammar, the same validator, the same errors — an inline subquery
    is a query, not a second language. Every node reports the document line the
    clause sits on (`fixed_line`), so a mistake inside it points at real text.
    `outer` names identifiers the body may read from outside itself, which is
    what makes a lateral body lateral; it is *this* body's, and not inherited by
    a subquery nested inside it, because the engine correlates through the
    immediate body's `\\where` alone.
    """
    commands = split_commands(body)
    if not commands:
        empty = QueryAST()
        if line != opts.typing_line:
            empty.errors.append(QueryError(line=line, message="a subquery body cannot be empty"))
        return empty
    return parse_slice(
        commands,
        ParseOpts(
            schema=opts.schema,
            expansion_stack=opts.expansion_stack,
            expansion_budget=opts.expansion_budget,
            fixed_line=line,
            typing_line=opts.typing_line,
            visible_ctes=visible_ctes_of(opts, ()),
            top=False,
            outer_idents=list(outer or []),
        ),
    )


def absorb(ast: QueryAST, child: QueryAST) -> None:
    """A nested query's errors belong to the document; the nested AST keeps none."""
    ast.errors.extend(child.errors)
    child.errors = []


def visible_ctes_of(opts: ParseOpts, own: tuple[str, ...]) -> list[str]:
    """The CTE names a slice may reference: the enclosing ones, then its own."""
    return [*opts.visible_ctes, *own]


def parse_frame_bounds(text: str) -> FrameBounds | None:
    """`rows|range between <bound> and <bound>` → its two canonical bounds."""
    head = _FRAME_HEAD_RE.match(text.strip())
    if head is None:
        return None
    bounds: list[str] = []
    for raw in (head.group(2), head.group(3)):
        bound = raw.strip().lower()
        if _FRAME_BOUND_RE.match(bound) is None or len(bound.split()[0]) > MAX_NUMERIC_CHARS:
            return None
        # `2  preceding` -> `2 preceding`: one spelling per bound in the AST.
        bounds.append(re.sub(r"\s+", " ", bound))
    return FrameBounds(kind=head.group(1).lower(), start=bounds[0], end=bounds[1])


def parse_source(text: str) -> tuple[str, str | None, str | None] | None:
    """`<dataset> [as] <alias>` or `( <commands> ) [as] <alias>` → its three parts.

    Returns `(dataset, body_text, alias)`: `dataset` is empty when the source is
    an inline subquery, whose `body_text` is then set. `None` when the text does
    not parse — for a `\\join` the caller separates the `on …` tail first.
    """
    rest = text.strip()
    dataset, body = "", None
    if rest.startswith("("):
        paren = take_paren(rest)
        if paren is None:
            return None
        body, rest = paren[0], paren[1]
    else:
        m = re.match(rf"^({_IDENT})", rest)
        if m is None:
            return None
        dataset, rest = m.group(1), rest[m.end() :].strip()
    alias: str | None = None
    am = re.match(rf"^(?:as\s+)?({_IDENT})$", rest, re.IGNORECASE)
    if am is not None:
        alias = am.group(1)
        rest = ""
    if rest.strip():
        return None
    return dataset, body, alias


def is_identifier(text: str) -> bool:
    """Whether `text` is exactly one bare identifier (a function or param name)."""
    return re.fullmatch(_IDENT, text) is not None


def param_spans(text: str) -> list[tuple[int, int, str]]:
    """`@name` parameter tokens outside quotes, with their spans.

    Quote-aware like the rest of the scanner family, so `email@host` inside a
    quoted value is never a parameter — and a token glued to a word (`a@b`) is
    skipped for the same reason. Inside parens it *is* a token (`limit(@n)`).
    """
    out: list[tuple[int, int, str]] = []
    quote: str | None = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote is not None:
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            i += 1
            continue
        if ch == "@" and (i == 0 or not re.match(r"[A-Za-z0-9_]", text[i - 1])):
            m = re.match(rf"@({_IDENT})", text[i:])
            if m is not None:
                out.append((i, i + m.end(), m.group(1)))
                i += m.end()
                continue
        i += 1
    return out


def substitute_params(body: str, args: dict[str, str]) -> str | None:
    """Replace every `@name` token in a body with its argument text verbatim.

    `None` when the body names a parameter with no argument. The substitution
    is textual on purpose: the engine sees literals, and ibis never learns a
    function existed.
    """
    spans = param_spans(body)
    if not spans:
        return body
    out: list[str] = []
    cursor = 0
    for start, end, name in spans:
        raw = args.get(name)
        if raw is None:
            return None
        out.append(body[cursor:start])
        out.append(raw)
        cursor = end
    out.append(body[cursor:])
    return "".join(out)


def balanced_function_text(text: str) -> bool:
    """Quoted delimiters are values; only unquoted parentheses affect nesting."""
    quote: str | None = None
    depth = 0
    for char in text:
        if quote is not None:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return False
    return quote is None and depth == 0


def expand_function_body(body: str, args: dict[str, str]) -> list[str]:
    """Bind within existing commands, never re-scan their boundaries after binding."""
    if not balanced_function_text(body):
        raise ValueError("unbalanced quotes or parentheses in function body")
    if any(not balanced_function_text(arg) for arg in args.values()):
        raise ValueError("unbalanced quotes or parentheses in function argument")
    commands: list[str] = []
    for command in split_commands(body):
        expanded = substitute_params(command, args)
        if expanded is None:
            missing = next(name for _, _, name in param_spans(command) if name not in args)
            raise ValueError(f'unknown parameter "@{missing}"')
        if not balanced_function_text(expanded):
            raise ValueError("argument leaves unbalanced quotes or parentheses in function body")
        commands.append(expanded)
    return commands


def parse_fn_source(
    rest: str,
    line: int,
    opts: ParseOpts,
    outer: list[str] | None = None,
    allow_alias: bool = True,
) -> tuple[str, QueryAST | None, str, str] | None:
    r"""`<fn>(<args>) [as] <alias>` at a source position → the call's expansion.

    `None` means the text is not a call this registry can answer (the site
    parses it as a plain source, exactly as before an entry exists). Otherwise
    `(name, body, alias, message)`: a non-empty `message` is why the call was
    refused; `body` is the substituted body parsed as a relation — the same
    machinery as an inline subquery, on the call's line — and `alias` defaults
    to the function name, so the body's columns qualify through it the way a
    dataset's would.

    An argument is a balanced value, never a command: text containing `\`
    is refused rather than spliced in.
    """
    head = re.match(rf"^({_IDENT})\s*\(", rest)
    if head is None:
        return None
    name = head.group(1)
    fn = opts.schema.fn_by_name(name)
    known = opts.schema.fns
    if fn is None:
        if not known:
            # Pure-parse regime: no registry loaded means no functions to find;
            # the site's own dataset-name error is the honest one here.
            return None
        return (name, None, "", f'unknown function "{name}" — defined: ' + ", ".join(t.name for t in known))
    if name in opts.expansion_stack:
        chain = " → ".join((*opts.expansion_stack, name))
        return (name, None, "", f"recursive function call: {chain}")
    if len(opts.expansion_stack) >= MAX_FUNCTION_DEPTH:
        return (name, None, "", f"function expansion exceeds {MAX_FUNCTION_DEPTH} nested calls")
    if opts.expansion_budget.remaining == 0:
        return (name, None, "", f"query exceeds {MAX_FUNCTION_EXPANSIONS} function expansions")
    opts.expansion_budget.remaining -= 1
    paren = take_paren(rest[head.end() - 1 :])
    if paren is None:
        return (name, None, "", f'a function call needs a closing ")" — {name}(...)')
    args = split_top(paren[0], keep_empty=True) if paren[0].strip() else []
    if any(not arg for arg in args):
        return (name, None, "", f"{name}() cannot contain an empty argument")
    if len(args) != len(fn.params):
        if not fn.params:
            takes = "takes no arguments"
        else:
            plural = "s" if len(fn.params) != 1 else ""
            takes = f"takes {len(fn.params)} argument{plural}"
        got = f"got {len(args)}" if args else "got none"
        return (name, None, "", f"{name}() {takes} — {got}")
    if any("\\" in a for a in args):
        return (
            name,
            None,
            "",
            f"an argument to {name}() cannot contain \\ — write a value or an inline ( \\from … ) subquery",
        )
    alias = name
    if paren[1]:
        am = re.match(rf"^(?:as\s+)?({_IDENT})$", paren[1], re.IGNORECASE)
        if am is None:
            return (name, None, "", f'cannot parse "{rest}"')
        if not allow_alias:
            return (name, None, "", "a set-operation source cannot be aliased")
        alias = am.group(1)
    try:
        commands = expand_function_body(fn.body, dict(zip(fn.params, args)))
    except ValueError as exc:
        return (name, None, "", f'function "{name}": {exc}')
    if not commands:
        return (name, None, "", f'function "{name}" has no query body')
    body_ast = parse_slice(
        commands,
        ParseOpts(
            schema=opts.schema,
            expansion_stack=(*opts.expansion_stack, name),
            expansion_budget=opts.expansion_budget,
            fixed_line=line,
            typing_line=opts.typing_line,
            # A saved body is its own document: it references datasets, never
            # the caller's CTEs.
            visible_ctes=[],
            top=False,
            outer_idents=list(outer or []),
        ),
    )
    return (name, body_ast, alias, "")


def parse_body(
    text: str,
    params: Sequence[str],
    *,
    schema: SchemaContext = EMPTY_SCHEMA,
    function_name: str | None = None,
) -> list[str]:
    """Why a FN body text is not a usable relation, as error messages.

    The editor runs this on save so a function that would only fail at a call
    site is refused where it is written. The body is parsed exactly as a call
    expands it (same grammar, same `top=False` regime that refuses `\\temp`,
    `\\drop`, transactions, and nested CTEs), against the *live* schema so its
    own column references are checked. Every `@token` is blanked first so the
    grammar parses, and one that `params` does not declare is named — a call
    could never bind it. An empty list means the body is a well-formed relation
    whose every parameter is declared.
    """
    messages: list[str] = []
    used = [name for _, _, name in param_spans(text)]
    for name in dict.fromkeys(used):
        if name not in params:
            messages.append(f'unknown parameter "@{name}" — declare it or remove it')
    try:
        commands = expand_function_body(text, {name: "0" for name in used})
    except ValueError as exc:
        return [*messages, str(exc)]
    if not commands:
        messages.append("a function body needs a query (a `\\from` line)")
        return messages
    ast = parse_slice(commands, ParseOpts(
        schema=schema,
        expansion_stack=(function_name,) if function_name else (),
        expansion_budget=ExpansionBudget(MAX_FUNCTION_EXPANSIONS - int(function_name is not None)),
        fixed_line=1, typing_line=0, visible_ctes=[], top=False,
    ))
    if ast.from_ is None and not ast.errors:
        messages.append("a function body needs a query (a `\\from` line)")
    messages.extend(error.message for error in ast.errors)
    return messages


def parse_regex_call(fn: str, args: list[str]) -> RegexCall | None:
    """`regexp_extract(<col>, <pattern>[, <group>])` / `regexp_replace(<col>, <pattern>, <replacement>)`.

    `None` when the arguments do not fit the function — the caller names the shape.
    """
    if not args or not _COL_RE.match(args[0]):
        return None
    column = args[0]
    if fn == "regexp_extract":
        if len(args) not in (2, 3) or not unquote(args[1]):
            return None
        group: int | None = None
        if len(args) == 3:
            if not _LIMIT_RE.match(args[2].strip()):
                return None
            group = checked_integer(args[2].strip(), "regex capture group")
        return RegexCall(fn="regexp_extract", arg=column, pattern=unquote(args[1]), group=group)
    if len(args) != 3 or not unquote(args[1]):
        return None
    return RegexCall(
        fn="regexp_replace",
        arg=column,
        pattern=unquote(args[1]),
        replacement=unquote(args[2]),
    )


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
        cond_match = _WHERE_HEAD_RE.match(cond)
        if cond_match is None:
            return None
        # A `\case` branch carries a literal comparison (regex operators
        # included); the subquery operators have no operand there.
        op = re.sub(r"\s+", " ", cond_match.group(2).lower())
        if op in SUBQUERY_OPS or not cond_match.group(3).strip():
            return None
        whens.append(
            CaseBranch(
                column=cond_match.group(1),
                op=op,
                value=unquote(cond_match.group(3)),
                then=unquote(then_val),
            )
        )
    if not whens:
        return None
    return CaseClause(line=0, raw=rest, alias=alias, whens=whens, else_=else_value)


def parse_over_frame(tail_raw: str) -> WindowFrame | None:
    """Consume partition columns, ordering, then frame bounds in grammar order."""
    tail = tail_raw.strip()
    partition_by: list[str] = []
    order: WindowOrder | None = None
    bounds: FrameBounds | None = None
    pos = 0
    partition = _PARTITION_START_RE.match(tail)
    if partition is not None:
        pos = partition.end()
        while True:
            column = _WINDOW_COLUMN_RE.match(tail, pos)
            if column is None:
                return None
            partition_by.append(column.group())
            pos = column.end()
            separator = _PAREN_SPLIT_RE.match(tail, pos)
            if separator is None:
                break
            pos = separator.end()
            if pos == len(tail) or _ORDER_START_RE.match(tail, pos) or _FRAME_START_RE.match(tail, pos):
                break
    ordering = _WINDOW_ORDER_RE.match(tail, pos)
    if ordering is not None:
        direction = ordering.group(2)
        order = WindowOrder(column=ordering.group(1), direction=direction.lower() if direction else "asc")
        pos = ordering.end()
    remainder = tail[pos:].strip()
    if remainder:
        bounds = parse_frame_bounds(remainder)
        if bounds is None or order is None:
            return None
    if not partition_by and order is None:
        return None
    return WindowFrame(partition_by=partition_by, order=order, frame=bounds)


def over_frame_error(text: str) -> str:
    """Why `over ( … )` did not parse.

    A spelled frame is a shape of its own — an ordering to count from and two
    bounds — so it gets its own message instead of the partition/order one.
    """
    if _FRAME_START_RE.search(text):
        return (
            "over (...) frame needs order by <col> [asc|desc] and bounds of "
            "`<n> preceding`, `current row`, or `<n> following` "
            "(either end may be `unbounded preceding` / `unbounded following`)"
        )
    return "over (...) needs partition by <cols> and/or order by <col> [asc|desc]"


def block_extent(lines: list[str], header: int) -> int:
    """The index one past the last line of the `\\with` block at `header`.

    The body is every indented line under the header, and blank lines only
    while the next non-blank line is indented. One implementation, shared by the
    parser's `\\with` scan and by `clause_line`, so no caller invents a second.
    """
    j = header + 1
    while j < len(lines):
        if not lines[j].strip():
            # Inspect this blank run once, preserving its start if it ends the block.
            blank_start = j
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j == len(lines) or not _INDENT_RE.match(lines[j]):
                return blank_start
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

    `\\from`/`\\open` and `\\distinct`/`\\unique` share clause identities;
    a repeated clause answers with its LAST line — the one a new item extends.
    `None` means this block has no such clause. The palette uses this so
    that asking for a clause never writes a second one beside the first.
    """
    lines = doc.split("\n")
    line = max(1, min(line, len(lines)))
    start, end = block_bounds(lines, line)
    wanted = COMMAND_ALIASES.get(command.lower(), command.lower())
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
        if COMMAND_ALIASES.get(name, name) == wanted:
            found = index + 1
        index += 1
    return found


@dataclass
class ExpansionBudget:
    # Shared by every nested slice: branching expansion must also be bounded.
    remaining: int = MAX_FUNCTION_EXPANSIONS


@dataclass
class ParseOpts:
    schema: SchemaContext = EMPTY_SCHEMA
    expansion_stack: tuple[str, ...] = ()
    expansion_budget: ExpansionBudget = field(default_factory=ExpansionBudget)
    # Line-number offset: body lines report their absolute document line.
    offset: int = 0
    # Absolute 1-based line being typed (errors suppressed there); 0 = none.
    typing_line: int = 0
    # CTE names visible to this slice (defined before it, in document order).
    visible_ctes: list[str] = field(default_factory=list)
    # False inside a `\with` body or an inline subquery: nested CTEs, a nested
    # `\with`, and the statement-level commands are errors there.
    top: bool = True
    # Set for an inline `( … )` subquery: every node reports this document line,
    # because the whole subquery sits on the clause's one line.
    fixed_line: int | None = None
    # A lateral body's visible outer identifiers: the tables accumulated to its
    # left, which only a lateral body may read.
    outer_idents: list[str] = field(default_factory=list)


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
        line = opts.fixed_line if opts.fixed_line is not None else i + 1 + opts.offset
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
                    schema=opts.schema,
                    expansion_stack=opts.expansion_stack,
                    expansion_budget=opts.expansion_budget,
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
            if opts.schema.tables and name not in opts.visible_ctes and opts.schema.table_by_name(name):
                # The CTE name shadows nothing, that's fine; only a dataset
                # collision is worth flagging (the dataset becomes unreachable).
                err(f'CTE name "{name}" shadows dataset "{name}"')
            continue

        if cmd in ("from", "open"):
            # A registered call `\from <fn>(<args>)` expands to its body with
            # the arguments substituted for `@params` — before anything else
            # reads the text, so the AST, payload, and engine see a body.
            fn = parse_fn_source(rest, line, opts)
            if fn is not None:
                name, fn_body, alias, message = fn
                if message:
                    err(message)
                    i += 1
                    continue
                absorb(ast, fn_body)
                ast.from_ = FromClause(line=line, table="", alias=alias, body=fn_body)
                i += 1
                continue
            src = parse_source(rest)
            if src is None:
                err(f"\\{cmd} expects a dataset name")
                i += 1
                continue
            table, body_text, alias = src
            body: QueryAST | None = None
            if body_text is not None:
                if alias is None:
                    err("a subquery source needs an alias — write ( … ) as <name>")
                    i += 1
                    continue
                body = parse_subquery(body_text, line, opts)
                absorb(ast, body)
            ast.from_ = FromClause(line=line, table=table, alias=alias, body=body)
            # Only complain about unknown tables once the schema has loaded —
            # while the backend is offline the registry is legitimately empty.
            # CTE names defined earlier in the document count as known tables.
            if (
                body is None
                and opts.schema.tables
                and not opts.schema.table_by_name(table)
                and table not in visible_ctes()
            ):
                err(
                    f'unknown table "{table}" — loaded datasets: '
                    + ", ".join(t.name for t in opts.schema.tables)
                )
            i += 1
            continue

        if cmd == "join":
            # `\join lateral ( … ) …` is the one source shape whose body may read
            # the left side; the `on …` tail is cut first so a dataset name, a
            # subquery body, and `on` cannot be mistaken for one another.
            lateral_match = re.match(r"^lateral\s+", rest, re.IGNORECASE)
            lateral = lateral_match is not None
            clause = rest[lateral_match.end() :].strip() if lateral_match else rest
            on_at = keyword_positions(clause, "on")
            source_text, on_text = clause, ""
            if on_at:
                source_text, on_text = clause[: on_at[-1]].strip(), clause[on_at[-1] :].strip()
            # A registered call at the source position expands like a lateral
            # subquery whose body is the function's, arguments substituted.
            outer = [
                t.identifier for t in open_tables_of(ast.from_, ast.joins)
            ] if lateral else None
            fn = parse_fn_source(source_text, line, opts, outer)
            if fn is not None:
                name, fn_body, alias, message = fn
                if not message and lateral:
                    # Same ambiguity rule as a hand-written lateral body.
                    clash = next(
                        (
                            t.identifier
                            for t in open_tables_of(fn_body.from_, fn_body.joins)
                            if t.identifier in (outer or [])
                        ),
                        "",
                    )
                    if clash:
                        message = (
                            f'duplicate table identifier "{clash}" — '
                            "a lateral body cannot reuse a name from its left"
                        )
                if message:
                    err(message)
                    i += 1
                    continue
                absorb(ast, fn_body)
                fn_left = fn_right = ""
                if on_text:
                    fn_on = _JOIN_ON_RE.match(on_text)
                    if fn_on is None:
                        err(
                            "\\join lateral expects `( <subquery> ) [as] <alias> [on <col>[ = <col>]]`"
                            if lateral
                            else "\\join expects `<dataset> [as] <alias> on <col>[ = <col>]`"
                        )
                        i += 1
                        continue
                    fn_left = fn_on.group(1)
                    fn_right = fn_on.group(2) or fn_on.group(1)
                elif not lateral:
                    err("\\join expects `<dataset> [as] <alias> on <col>[ = <col>]`")
                    i += 1
                    continue
                ast.joins.append(
                    JoinClause(
                        line=line,
                        dataset="",
                        alias=alias,
                        left=fn_left,
                        right=fn_right,
                        lateral=lateral,
                        body=fn_body,
                    )
                )
                i += 1
                continue
            src = parse_source(source_text)
            if src is None or (lateral and src[1] is None):
                err(
                    "\\join lateral expects `( <subquery> ) [as] <alias> [on <col>[ = <col>]]`"
                    if lateral
                    else "\\join expects `<dataset> [as] <alias> on <col>[ = <col>]`"
                )
                i += 1
                continue
            table, body_text, alias = src
            body = None
            if body_text is not None:
                if alias is None:
                    err("a subquery source needs an alias — write ( … ) as <name>")
                    i += 1
                    continue
                # A lateral body reads the identifiers accumulated to its left.
                body = parse_subquery(body_text, line, opts, outer)
                absorb(ast, body)
                if outer:
                    # One identifier cannot mean two tables: a body table named
                    # like one to its left would make every reference to it
                    # ambiguous, so it is refused where it is written.
                    clash = next(
                        (
                            t.identifier
                            for t in open_tables_of(body.from_, body.joins)
                            if t.identifier in outer
                        ),
                        "",
                    )
                    if clash:
                        err(
                            f'duplicate table identifier "{clash}" — '
                            "a lateral body cannot reuse a name from its left"
                        )
                        i += 1
                        continue
            left_ref = right_ref = ""
            if on_text:
                on_match = _JOIN_ON_RE.match(on_text)
                if on_match is None:
                    err(
                        "\\join lateral expects `( <subquery> ) [as] <alias> [on <col>[ = <col>]]`"
                        if lateral
                        else "\\join expects `<dataset> [as] <alias> on <col>[ = <col>]`"
                    )
                    i += 1
                    continue
                left_ref = on_match.group(1)
                right_ref = on_match.group(2) or on_match.group(1)
            elif not lateral:
                err("\\join expects `<dataset> [as] <alias> on <col>[ = <col>]`")
                i += 1
                continue
            ast.joins.append(
                JoinClause(
                    line=line,
                    dataset=table,
                    alias=alias,
                    left=left_ref,
                    right=right_ref,
                    lateral=lateral,
                    body=body,
                )
            )
            # Like `\from`: only complain once the schema has loaded.
            if (
                body is None
                and opts.schema.tables
                and not opts.schema.table_by_name(table)
                and table not in visible_ctes()
            ):
                err(
                    f'unknown table "{table}" — loaded datasets: '
                    + ", ".join(t.name for t in opts.schema.tables)
                )
            i += 1
            continue

        if cmd in SET_OP_COMMANDS:
            clause = rest
            modifier = ""
            mm = re.match(r"^(all|distinct)\s+", clause, re.IGNORECASE)
            if mm is not None:
                modifier = mm.group(1).lower()
                clause = clause[mm.end() :].strip()
            body = None
            name = ""
            fn = parse_fn_source(clause, line, opts, allow_alias=False)
            if fn is not None:
                _, fn_body, _, message = fn
                if message:
                    err(message)
                    i += 1
                    continue
                absorb(ast, fn_body)
                body = fn_body
            elif clause.startswith("("):
                paren = take_paren(clause)
                if paren is None or paren[1]:
                    err(f"\\{cmd} expects `[all|distinct] <dataset>` or `( … )`")
                    i += 1
                    continue
                body = parse_subquery(paren[0], line, opts)
                absorb(ast, body)
            else:
                sm = _SET_OP_RE.match(clause)
                if sm is None or sm.group(2).lower() in SET_OP_MODIFIERS:
                    # `\union all` names nothing: a modifier is not the table.
                    err(f"\\{cmd} expects `[all|distinct] <dataset>` or `( … )`")
                    i += 1
                    continue
                name = sm.group(2)
            ast.set_ops.append(
                SetOpClause(
                    line=line,
                    op=cmd,  # type: ignore[arg-type]
                    dataset=name,
                    distinct=modifier != "all",
                    body=body,
                )
            )
            # Like `\from`/`\join`: only complain once the schema has loaded,
            # and a CTE defined earlier is a table for this purpose.
            if (
                body is None
                and opts.schema.tables
                and not opts.schema.table_by_name(name)
                and name not in visible_ctes()
            ):
                err(
                    f'unknown table "{name}" — loaded datasets: '
                    + ", ".join(t.name for t in opts.schema.tables)
                )
            i += 1
            continue

        if cmd in ("distinct", "unique"):
            if rest:
                err(f"\\{cmd} takes no arguments")
            else:
                ast.distinct = True
            i += 1
            continue

        if cmd == "select":
            # One `\select` line may carry several comma-separated expressions;
            # each segment parses exactly like a single-expression line. A
            # trailing `over ( … )` attaches a window frame to the expression.
            for expr in split_top(rest):
                item = SelectItem(line=line, raw=expr)
                # An inline `( … )` subquery is a whole expression, so it is read
                # before anything meant for an expression: the body's own ` as `
                # or ` over ( … )` must never be mistaken for this item's.
                paren = take_paren(expr)
                if paren is not None:
                    body_text, after = paren
                    alias_match = re.match(rf"^(?:as\s+)?({_IDENT})$", after, re.IGNORECASE)
                    item.subquery = parse_subquery(body_text, line, opts)
                    absorb(ast, item.subquery)
                    item.alias = alias_match.group(1) if alias_match else None
                    if after and alias_match is None:
                        err(f'cannot parse expression "{expr}"')
                    elif not item.alias:
                        err("a scalar subquery needs an alias — write ( … ) as <name>")
                    ast.select.append(item)
                    continue
                core = expr
                as_match = _AS_RE.match(expr)
                if as_match:
                    core = as_match.group(1).strip()
                    item.alias = as_match.group(2)
                over = _OVER_TAIL_RE.match(core)
                if over:
                    frame = parse_over_frame(over.group(2))
                    if frame is None:
                        err(over_frame_error(over.group(2)))
                        ast.select.append(item)
                        continue
                    item.window = frame
                    core = over.group(1).strip()
                try:
                    item.literal = parse_literal(core)
                except ValueError as exc:
                    err(str(exc))
                    ast.select.append(item)
                    continue
                if item.literal is not None:
                    if item.window:
                        err("a literal cannot carry over (...) — wrap it in a function")
                    ast.select.append(item)
                    continue
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
                regex = _REGEX_FN_RE.match(core)
                rank = _RANK_RE.match(core)
                scalar_head = _CALL_HEAD_RE.match(core)
                if scalar_head and scalar_head.group(1).lower() in SCALAR_FUNCTIONS:
                    try:
                        item.scalar = parse_scalar_call(core)
                    except ValueError as exc:
                        err(str(exc))
                    if item.window:
                        err(f'"{scalar_head.group(1).lower()}" is not a window function — use sum/avg/count/min/max')
                    ast.select.append(item)
                    continue
                if regex:
                    fn = regex.group(1).lower()
                    args = split_top(regex.group(2))
                    try:
                        call = parse_regex_call(fn, args)
                    except ValueError as exc:
                        err(str(exc))
                        ast.select.append(item)
                        continue
                    if call is None:
                        err(
                            "regexp_extract expects `<column>, <pattern>[, <group>]`"
                            if fn == "regexp_extract"
                            else "regexp_replace expects `<column>, <pattern>, <replacement>`"
                        )
                    else:
                        if item.window:
                            err(f'"{fn}" is not a window function — use sum/avg/count/min/max')
                        item.regex = call
                elif rank:
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
            wm = _WHERE_HEAD_RE.match(rest)
            if wm is None:
                err("\\where expects `column op value`")
                i += 1
                continue
            column = wm.group(1)
            op = re.sub(r"\s+", " ", wm.group(2).lower())
            tail = wm.group(3).strip()
            if not tail:
                err("\\where expects `column op value`")
                i += 1
                continue
            subquery: QueryAST | None = None
            value = tail
            # An operand that is a command sequence is a subquery; a bare
            # parenthesized literal (`= (3)`) stays the value it looks like, and
            # so does one under an operator that takes text (`like`, `~`).
            paren = take_paren(tail)
            is_subquery = paren is not None and "\\" in paren[0] and not paren[1]
            if op in SUBQUERY_OPS:
                if not is_subquery:
                    err(f"`{op}` expects an inline subquery — write ( \\from … )")
                    i += 1
                    continue
                subquery = parse_subquery(paren[0], line, opts)
                absorb(ast, subquery)
                value = ""
            elif is_subquery and op not in REGEX_OPS | {"like"}:
                subquery = parse_subquery(paren[0], line, opts)
                absorb(ast, subquery)
                value = ""
            else:
                value = unquote(tail)
            ast.where = WhereClause(
                line=line,
                raw=rest,
                column=column,
                op=op,
                value=value,
                subquery=subquery,
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
            try:
                ast.limit = checked_integer(rest, "limit")
            except ValueError as exc:
                err(str(exc))
            i += 1
            continue

        if cmd in ("temp", "drop"):
            nm = _WITH_NAME_RE.match(rest)
            if nm is None:
                err(f"\\{cmd} expects a bare table name")
                i += 1
                continue
            if not opts.top:
                err(f"\\{cmd} is only allowed in the document")
                i += 1
                continue
            # Whether the name is free is the session's to know (it owns the
            # source's datasets and the temp tables this session made), so the
            # parser only reads the name.
            if cmd == "temp":
                ast.temp = TempClause(line=line, name=nm.group(1))
            else:
                ast.drop = DropClause(line=line, name=nm.group(1))
            i += 1
            continue

        if cmd in TX_COMMANDS:
            if not opts.top:
                err(f"\\{cmd} is only allowed in the document")
                i += 1
                continue
            kind = TX_COMMANDS[cmd]
            name: str | None = None
            if cmd in TX_BARE:
                if rest:
                    err(f"\\{cmd} takes no arguments")
                    i += 1
                    continue
            elif cmd == "rollback":
                if rest:
                    to_match = re.match(rf"^to\s+({_IDENT})$", rest, re.IGNORECASE)
                    if to_match is None:
                        err("\\rollback expects `[to <savepoint>]`")
                        i += 1
                        continue
                    kind, name = "rollback_to", to_match.group(1)
            else:
                nm = _WITH_NAME_RE.match(rest)
                if nm is None:
                    err(f"\\{cmd} expects a savepoint name")
                    i += 1
                    continue
                name = nm.group(1)
            ast.tx.append(TxDirective(line=line, kind=kind, name=name))  # type: ignore[arg-type]
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
    if opts.schema.tables:
        open_idents = [t.identifier for t in open_tables_of(ast.from_, ast.joins)]
        # A lateral body may also read the identifiers outside it.
        known_idents = [*open_idents, *opts.outer_idents]

        def check(ref_line: int, ref: str) -> None:
            if ref_line == opts.typing_line or "." not in ref:
                return
            prefix = ref[: ref.index(".")]
            if prefix not in known_idents:
                listed = ", ".join(open_idents) or "(no \\from)"
                ast.errors.append(
                    QueryError(
                        line=ref_line,
                        message=f'unknown column "{ref}" — "{prefix}" is not an open table: {listed}',
                    )
                )

        def check_scalar(ref_line: int, call: ScalarCall) -> None:
            for arg in call.args:
                if isinstance(arg, ColumnRef):
                    check(ref_line, arg.column)
                elif isinstance(arg, ScalarCall):
                    check_scalar(ref_line, arg)

        for s in ast.select:
            check(s.line, s.column or "")
            if s.aggregate:
                check(s.line, s.aggregate.arg)
            if s.temporal:
                check(s.line, s.temporal.arg)
            if s.regex:
                check(s.line, s.regex.arg)
            if s.scalar:
                check_scalar(s.line, s.scalar)
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
        # A qualified `\join on` side names whichever relation it points at: the
        # accumulated left or this join's own table. `on a.k = b.k` and
        # `on b.k = a.k` are the same equality, so both operands are checked
        # against the identifiers open AT this clause — everything before it plus
        # the alias the clause itself establishes (which the engine resolves too).
        sofar: list[str] = [ast.from_.alias or ast.from_.table] if ast.from_ else []
        for j in ast.joins:
            open_here = [*sofar, j.alias or j.dataset]
            for side in (j.left, j.right):
                if "." not in side:
                    continue
                prefix = side[: side.index(".")]
                if j.line != opts.typing_line and prefix not in open_here:
                    listed = ", ".join(open_here) or "(no \\from)"
                    ast.errors.append(
                        QueryError(
                            line=j.line,
                            message=f'unknown column "{side}" — "{prefix}" is not an open table: {listed}',
                        )
                    )
            sofar = open_here

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


def parse_query(doc: str, *, schema: SchemaContext = EMPTY_SCHEMA, settled: bool = False) -> QueryAST:
    """Parse text against one immutable schema; explicit submissions are settled."""
    lines = doc.split("\n")
    last_content_line = 0
    for i, raw in enumerate(lines):
        if raw.strip():
            last_content_line = i + 1
    return parse_slice(lines, ParseOpts(
        schema=schema, typing_line=0 if settled else last_content_line,
    ))


def payload_from_ast(ast: QueryAST) -> dict:
    """QueryAST → the engine payload, 1:1 with the wire contract.

    An empty select list means "all columns", `\\join` clauses pass through in
    document order (always a list, possibly empty), `\\order` targets pass
    through verbatim (the engine resolves aliases first, then raw columns),
    `\\limit` maps to `limit`. `\\union`/`\\intersect`/`\\except` pass through as
    `setOps` in document order (always a list, possibly empty) with SQL's
    deduplicating default baked in (`distinct`). Column strings may be qualified
    (`users.score`, `e.user_id` — identifiers, not necessarily dataset names) and reach the
    engine untouched. Table aliases ride along (`alias`, always present, `None`
    when unaliased). Aggregate select items carry the derived auto-alias
    (`auto_alias`) when the user gave none — the engine re-derives the same
    name for null aliases; sending it up front keeps the two layers identical.
    `\\distinct`/`\\unique` normalize to the query's always-present `distinct`
    boolean, independently of each set operation's deduplication flag.

    Inline `( … )` subqueries embed as full payloads of their own (`body` on a
    source/join/set-op, `subquery` on a select item or `\\where`), and the
    statements the query cannot carry — `\\temp`, `\\drop`, the transaction
    commands — ride along as `temp`, `drop` and `tx`, because running them is
    the session's job, not the expression builder's.
    """
    return {
        "dataset": ast.from_.table if ast.from_ else "",
        "alias": ast.from_.alias if ast.from_ else None,
        "body": payload_from_ast(ast.from_.body) if ast.from_ and ast.from_.body else None,
        "joins": [
            {
                "dataset": j.dataset,
                "alias": j.alias,
                "left": j.left,
                "right": j.right,
                "lateral": j.lateral,
                "body": payload_from_ast(j.body) if j.body else None,
            }
            for j in ast.joins
        ],
        "setOps": [
            {
                "op": s.op,
                "dataset": s.dataset,
                "distinct": s.distinct,
                "body": payload_from_ast(s.body) if s.body else None,
            }
            for s in ast.set_ops
        ],
        "select": [
            {
                "column": s.column,
                "literal": s.literal.to_json() if s.literal is not None else None,
                "scalar": s.scalar.to_json() if s.scalar else None,
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
                        "frame": (
                            {
                                "kind": s.window.frame.kind,
                                "start": s.window.frame.start,
                                "end": s.window.frame.end,
                            }
                            if s.window.frame
                            else None
                        ),
                    }
                    if s.window
                    else None
                ),
                "regex": (
                    {
                        "fn": s.regex.fn,
                        "arg": s.regex.arg,
                        "pattern": s.regex.pattern,
                        "group": s.regex.group,
                        "replacement": s.regex.replacement,
                    }
                    if s.regex
                    else None
                ),
                "subquery": payload_from_ast(s.subquery) if s.subquery else None,
                "alias": effective_alias(s),
            }
            for s in ast.select
        ],
        "distinct": ast.distinct,
        "where": (
            {
                "column": ast.where.column,
                "op": ast.where.op,
                "value": ast.where.value,
                "subquery": payload_from_ast(ast.where.subquery) if ast.where.subquery else None,
            }
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
        "temp": ast.temp.name if ast.temp else None,
        "drop": ast.drop.name if ast.drop else None,
        "tx": [{"kind": t.kind, "name": t.name} for t in ast.tx],
        # A CTE body is a full payload minus its own (empty, by grammar) `with` list.
        "ctes": [{"name": c.name, "body": payload_from_ast(c.body)} for c in ast.with_],
    }
