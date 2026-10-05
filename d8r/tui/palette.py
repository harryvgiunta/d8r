"""The `\\` command palette: what to offer for the text being typed.

Two halves, deliberately apart:

* `view_for(...)` — pure: given the document, the caret's line and column, it
  returns the suggestions and the exact span an accepted suggestion replaces
  (or `None` for "no palette here"). Every rule the palette follows is testable
  without a terminal.
* `CommandPalette` — the overlay widget. It never takes focus: the editor keeps
  the caret and routes the keys the palette needs, so plain typing is never
  swallowed and the popup can sit over the document without stealing the
  keyboard.

Matching is the case-insensitive substring search the earlier web IDE used
(`tomer` finds `customer_totals`): an exact hit ranks first, then a prefix, then
a hit anywhere inside — and within one rank the declaration order stands. A
`\\select` expression offers fields before the functions that apply to them,
and an open call (`sum(am`) offers that function's argument instead. A `\\where`
value is searched over the whole distinct pool the session cached, of which at
most `VALUE_SUGGESTIONS` are shown, so a value past the first page of sorted
ones is still found by typing part of it.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Literal

from textual import events, on
from textual.binding import Binding
from textual.containers import Vertical
from textual.content import Content
from textual.message import Message
from textual.widgets import OptionList, TextArea
from textual.widgets.option_list import Option

from d8r.engine import DataSource
from d8r.query import (
    AGGREGATES,
    NULL_OPS,
    REGEX_OPS,
    SUBQUERY_OPS,
    TEMPORAL,
    SchemaContext,
    clause_line,
    dtype_family,
    is_identifier,
    param_spans,
    parse_query,
    split_logic,
    take_paren,
    where_head,
)
from d8r.query.functions import ARITHMETIC_PRECEDENCE, MAX_EXPRESSION_DEPTH, SCALAR_FUNCTIONS, ArgumentKind, accepts_type
from d8r.query.parser import block_bounds, keyword_positions, parse_source
from d8r.query.identifiers import IDENTIFIER, RELATION_NAME, relation_parts

from .session import Session, looks_numeric

# The clause commands, in document order, with the detail line the palette shows.
COMMANDS: tuple[tuple[str, str], ...] = (
    ("from", "set the source dataset"),
    ("open", "same as \\from — the dataset spelling"),
    ("join", "append an inner join"),
    ("union", "append a set union"),
    ("intersect", "keep only rows both sides have"),
    ("except", "subtract the other table's rows"),
    ("select", "append a projection"),
    ("distinct", "remove duplicate output rows"),
    ("unique", "same as \\distinct — remove duplicate output rows"),
    ("where", "set the row filter"),
    ("group", "append a grouping key"),
    ("order", "append an ordering"),
    ("case", "add a computed column"),
    ("limit", "cap the rows"),
    ("with", "start a CTE block"),
    ("temp", "keep this query as a temp table"),
    ("drop", "drop a temp table"),
    ("begin", "open a transaction"),
    ("savepoint", "mark a savepoint"),
    ("release", "release a savepoint"),
    ("rollback", "roll back the transaction"),
    ("commit", "commit the transaction"),
)

# Palette actions: label, detail, and the action name the app carries out.
# Actions are the app's own — they never enter the document or the AST.
ACTIONS: tuple[tuple[str, str, str], ...] = (
    ("Run", "ctrl+enter · execute the document", "run"),
    ("Compile", "ctrl+k · render SQL without running", "compile"),
    ("Data source…", "ctrl+o · add PostgreSQL, D1 or SQLite", "add-source"),
    ("Functions…", "\\fn · open or create a table-valued function", "fn"),
    ("Query to function", "create a function draft from this document", "query-to-fn"),
    ("Export results", "save selected or buffered rows as CSV", "export-results"),
    ("AI", "chat and review an AI-proposed replacement", "ai"),
    ("History", "show/hide the history pane", "history"),
    ("Pages", "show/hide the query pages pane", "toggle_pages"),
    ("Queries", "show/hide the query pages pane", "toggle_pages"),
    ("Results", "show/hide the results pane", "toggle-results"),
    ("SQL", "show/hide the SQL pane", "toggle-sql"),
    ("Schema", "show/hide the schema pane", "toggle-schema"),
    ("Settings", "every setting, full screen", "settings"),
)

# Commands whose argument is a dataset name, and whose argument is a column name.
DATASET_COMMANDS = frozenset({"from", "open", "join"})
COLUMN_COMMANDS = frozenset({"select", "group", "order"})

# The set-operation commands; their argument is `[all|distinct] <dataset>` — the
# modifier leads, and the name is what is being completed either way.
SET_OP_COMMANDS = frozenset({"union", "intersect", "except"})
SET_OP_MODIFIERS: tuple[tuple[str, str], ...] = (
    ("all", "keep duplicate rows"),
    ("distinct", "drop duplicate rows"),
)
SET_OP_MODIFIER_WORDS = frozenset(name for name, _ in SET_OP_MODIFIERS)

# The commands only a savepoint-capable engine can run: the palette offers them
# where the active source advertises `supports.savepoints` (SQLite has them;
# DuckDB keeps whole transactions only).
SAVEPOINT_COMMANDS = frozenset({"savepoint", "release"})

# The clause commands: accepting one takes its own line, or the line the
# current block already gives that clause.
CLAUSE_NAMES = frozenset(name for name, _ in COMMANDS)

# The function menu: saved entries insert calls, and explicit creation opens
# the library. The `\fn` menu text itself never remains in the document.
FN_COMMAND = "fn"

# Fallback comparison operators; the live capability set is preferred.
DEFAULT_OPERATORS: tuple[str, ...] = (
    "=", "!=", ">", ">=", "<", "<=", "like", "ilike", "in", "not in",
    "between", "is null", "is not null",
)

# The words that join two conditions; `between`'s own `and` is not one of them
# (the parser's `split_logic` masks it), so a bound never reads as a joiner.
_JOINERS: tuple[str, ...] = ("and", "or")
_COL_PATTER = re.compile(rf"(?:{RELATION_NAME}\.)?{IDENTIFIER}")
_NUMBER_RE = re.compile(r"^-?(?:\d+(?:\.\d*)?|\.\d+)$")

# How many value rows the popup shows; the search still reads the whole pool.
VALUE_SUGGESTIONS = 50

_CALL_NAME_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*$")


def operators(schema: SchemaContext) -> tuple[str, ...]:
    """The operators the active backend advertises for `\\where` and `\\case`."""
    live = schema.capabilities.operators
    return live or DEFAULT_OPERATORS


@dataclass(frozen=True)
class Entry:
    """One palette row: what it reads as, and what accepting it does."""

    label: str
    insert: str = ""
    detail: str = ""
    action: str = ""
    # Characters an accept leaves behind the caret — a call completes to `fn(|)`.
    cursor_back: int = 0


@dataclass(frozen=True)
class View:
    """The palette's current content and the document span an accept replaces."""

    start: int
    token: str
    entries: list[Entry] = field(default_factory=list)
    # "command": the word right after the `\\`. "argument": everything a command
    # takes. Escape's dismissal silences both; a fresh `\\` is what lifts it.
    phase: Literal["command", "argument"] = "command"
    # Replace the whole token when the caret is inside an existing argument.
    end: int | None = None

    @property
    def labels(self) -> list[str]:
        return [entry.label for entry in self.entries]

    def highlighted_is_typed(self, index: int) -> bool:
        """Whether the highlighted text is complete (Enter should be a newline)."""
        entry = self.entries[index]
        if entry.action:
            return False
        return entry.insert.strip().lstrip("\\") == self.token.strip()


def match_rank(text: str, token: str) -> int:
    """0 exact, 1 prefix, 2 substring — or -1 when `token` misses `text`.

    Case-insensitive, and an empty token matches everything at rank 0.
    """
    if not token:
        return 0
    folded = token.lower()
    lowered = text.lower()
    if lowered == folded:
        return 0
    if lowered.startswith(folded):
        return 1
    return 2 if folded in lowered else -1


def _dataset_match_rank(name: str, token: str) -> int:
    """Match relation components, including an unfinished quoted component."""
    parts = relation_parts(name)
    if parts is None or not any(char in token for char in '."'):
        return match_rank(name, token)
    typed = [""]
    quoted = False
    index = 0
    while index < len(token):
        char = token[index]
        if char == '"':
            if quoted and token[index:index + 2] == '""':
                typed[-1] += '"'
                index += 2
                continue
            quoted = not quoted
        elif char == "." and not quoted:
            typed.append("")
        else:
            typed[-1] += char
        index += 1
    if len(typed) > len(parts):
        return -1
    if len(typed) == 1:
        return min((rank for part in parts if 0 <= (rank := match_rank(part, typed[0])) <= 1), default=-1)
    if any(a.lower() != b.lower() for a, b in zip(parts, typed[:-1])):
        return -1
    rank = match_rank(parts[len(typed) - 1], typed[-1])
    return rank if rank <= 1 else -1


def _offers(
    rows: Iterable[tuple[int, str, str, str]], token: str, *, recency: dict[str, int] | None = None,
    qualified: bool = False, dataset: bool = False,
) -> list[Entry]:
    """(group, name, detail, insert) rows matching `token`, group then rank first.

    The group is the block a row belongs to (fields before functions); within a
    block the best match leads and equal ranks keep the order they were built in.
    """
    kept = [
        (group, rank, name, detail, insert)
        for group, name, detail, insert in rows
        if (rank := (_dataset_match_rank(name, token) if dataset else
                     match_rank(name.rsplit(".", 1)[-1] if qualified and "." not in token else name, token))) >= 0
    ]
    kept.sort(key=lambda row: (row[0], row[1], (recency or {}).get(row[2], float("inf"))))
    return [Entry(label=name, insert=insert, detail=detail) for _, _, name, detail, insert in kept]


def _history_offers(
    session: Session, doc: str, rows: Iterable[tuple[int, str, str, str]], token: str,
    *, category: str = "column",
) -> list[Entry]:
    return _offers(rows, token, recency=session.completion_ranks(doc, category=category),
                   qualified=category == "column", dataset=category == "dataset")


def prompt(label: str, detail: str = "") -> Content:
    """One menu row: the label, then its detail in a dim style."""
    if not detail:
        return Content(label)
    return Content.assemble(label, ("  " + detail, "dim"))


def _split(text: str) -> tuple[str, str, str]:
    """`text` as (word, whitespace gap, rest) — the command/argument split."""
    index = 0
    while index < len(text) and not text[index].isspace():
        index += 1
    word = text[:index]
    gap_end = index
    while gap_end < len(text) and text[gap_end].isspace():
        gap_end += 1
    return word, text[index:gap_end], text[gap_end:]


def _command_entries(session: Session, token: str, schema: SchemaContext) -> list[Entry]:
    """The language and the app's actions matching the word after the `\\`.

    What the backend advertises decides what is offered: `\\savepoint` and
    `\\release` need an engine with savepoints, so they are not offered on one
    without (DuckDB keeps whole transactions only). Typing them anyway is still
    the user's to do — the run refuses them by name.
    """
    supports = schema.capabilities.supports
    commands = [
        (match_rank(name, token), index, Entry(label=f"\\{name}", insert=f"\\{name} ", detail=detail))
        for index, (name, detail) in enumerate(COMMANDS)
        if (name not in SAVEPOINT_COMMANDS or supports.get("savepoints"))
        and (name not in {"distinct", "unique"} or supports.get("distinct"))
    ]
    actions = [
        (match_rank(label, token), len(COMMANDS) + index, Entry(label=label, detail=detail, action=action))
        for index, (label, detail, action) in enumerate(ACTIONS)
    ]
    functions = [
        (match_rank(call, token), len(COMMANDS) + len(ACTIONS) + index,
         Entry(label=f"\\{call}", insert=f"\\{call}", detail=detail, cursor_back=1))
        for index, (call, detail) in enumerate(session.fn_call_rows(schema=schema))
        if call[:-2].lower() not in CLAUSE_NAMES
    ]
    best = [row for row in (*commands, *actions, *functions) if row[0] >= 0]
    usage = session.completion_ranks("", category="command")
    best.sort(key=lambda row: (row[0], usage.get(row[2].label.lstrip("\\"), float("inf")), row[1]))
    return [entry for _, _, entry in best]


def _segment(rest: str) -> tuple[str, int]:
    """The comma-separated argument segment being typed, and its offset."""
    offset = rest.rfind(",") + 1
    raw = rest[offset:]
    return raw.lstrip(), offset + (len(raw) - len(raw.lstrip()))


@dataclass
class _ExpressionPosition:
    offset: int = 0
    fn: str | None = None
    argument: int = 0
    arithmetic: bool = False
    close: bool = True
    numeric_result: bool = False
    result_kind: ArgumentKind | None = None


def _expression_position(text: str) -> _ExpressionPosition | None:
    """Locate an operand without treating quoted operators or exponent signs as boundaries."""
    stack = [_ExpressionPosition()]
    quote: str | None = None
    for index, char in enumerate(text):
        current = stack[-1]
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "(":
            if len(stack) > MAX_EXPRESSION_DEPTH:
                return None
            # Dedicated calls (including aggregates) accept columns, not nested
            # calls or grouped arithmetic. Catalog calls accept expressions.
            if any(frame.fn and frame.fn not in SCALAR_FUNCTIONS for frame in stack):
                return None
            match = _CALL_NAME_RE.search(text[current.offset:index])
            fn = match.group(1).lower() if match else None
            if fn in AGGREGATES and any(frame.fn in SCALAR_FUNCTIONS for frame in stack):
                return None
            if current.arithmetic and fn and fn not in AGGREGATES:
                spec = SCALAR_FUNCTIONS.get(fn)
                if spec is None or spec.result not in {"integer", "float", "any"}:
                    return None
            parent = SCALAR_FUNCTIONS.get(current.fn)
            result_kind = parent.argument_kind(current.argument) if parent else None
            if parent and parent.result_argument == current.argument and current.result_kind is not None:
                result_kind = current.result_kind
            stack.append(_ExpressionPosition(
                index + 1, fn, arithmetic=fn is None,
                numeric_result=current.arithmetic,
                result_kind=result_kind,
            ))
        elif char == ")":
            if len(stack) > 1:
                stack.pop()
        elif char == ",":
            if len(stack) > 1 and current.fn is None:
                return None
            current.argument += 1
            current.offset = index + 1
            current.arithmetic = False
        elif char in ARITHMETIC_PRECEDENCE:
            if char in "+-" and re.fullmatch(
                r"\s*(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)[eE]", text[current.offset:index],
            ):
                continue
            current.offset = index + 1
            current.arithmetic = True
    if quote:
        return None
    current = stack[-1]
    owner = next((frame for frame in reversed(stack) if frame.fn is not None), stack[0])
    while current.offset < len(text) and text[current.offset].isspace():
        current.offset += 1
    return _ExpressionPosition(
        current.offset, owner.fn, owner.argument, current.arithmetic,
        close=current is owner and not current.arithmetic,
        numeric_result=owner.numeric_result,
        result_kind=owner.result_kind,
    )


def _word(rest: str) -> tuple[str, int]:
    """The word the caret is in (or right after) and its offset in `rest`.

    `\\group` and `\\where` take whitespace-separated arguments, so what is
    being completed there is the last word — not the whole comma segment.
    """
    if not rest or rest[-1].isspace():
        return "", len(rest)
    token = rest.split()[-1]
    return token, rest.rfind(token)


def _where_token(rest: str) -> tuple[str, int]:
    """Locate the operand being edited without splitting quoted words or groups."""
    start = 0
    quote: str | None = None
    for index, char in enumerate(rest):
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char.isspace() or char in "(),":
            start = index + 1
    return rest[start:], start


def _command_context(before: str) -> tuple[int, bool]:
    """Find the live command, ignoring quoted slashes and closed inner filters."""
    slash = -1
    stack: list[tuple[int, str | None]] = []
    quote: str | None = None
    for index, char in enumerate(before):
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "(":
            match = _CALL_NAME_RE.search(before, 0, index)
            stack.append((slash, match.group(1).lower() if match else None))
        elif char == ")" and stack:
            slash, _ = stack.pop()
        elif char == "\\":
            slash = index
    return slash, any(fn in AGGREGATES for _, fn in stack)


def _token_end(line: str, start: int, column: int, *, relation: bool = False) -> int:
    """Include a token's stale suffix, but never the next operand or punctuation."""
    if relation and start < column:
        quoted = False
        for index in range(start, len(line)):
            char = line[index]
            if char == '"':
                quoted = not quoted
            elif not quoted and (char.isspace() or char in ",()+-*/=<>!~\\"):
                return max(column, index)
        return len(line)
    if start < column and line[start] in "\"'":
        quote = line[start]
        index = start + 1
        while index < len(line):
            if line[index] == quote:
                if index + 1 < len(line) and line[index + 1] == quote:
                    index += 2
                    continue
                return max(column, index + 1)
            index += 1
        return column
    if start == column or not (line[column - 1].isalnum() or line[column - 1] in "_."):
        return column
    end = column
    while end < len(line) and (line[end].isalnum() or line[end] in "_."):
        end += 1
    return end


def _column_offers(session: Session, doc: str, token: str, schema: SchemaContext) -> list[Entry]:
    """The document's open-table columns as palette rows."""
    rows = [
        (0, name, detail, f"{name} ")
        for name, _, detail in session.column_entries(doc, schema=schema, qualified="." in token)
    ]
    return _history_offers(session, doc, rows, token)


def _aggregate_fns(schema: SchemaContext) -> list[str]:
    """The aggregates the active backend advertises that the language knows."""
    advertised = schema.capabilities.aggregates
    return [fn for fn in AGGREGATES if fn in advertised]


def _rank_fns(schema: SchemaContext) -> list[str]:
    """Rank functions — `<fn>() over ( ... )` with no argument of their own."""
    advertised = schema.capabilities.window_functions
    return [fn for fn in ("rank", "dense_rank", "row_number") if fn in advertised]


def _temporal_fns(columns: list[tuple[str, str, str]], schema: SchemaContext) -> list[str]:
    """Temporal extractions at least one of the open columns can carry."""
    families = {dtype_family(type_) for _, type_, _ in columns}
    return [
        fn
        for fn in TEMPORAL
        if any(fn in schema.capabilities.functions.get(family, ()) for family in families if family)
    ]


def _scalar_fns(schema: SchemaContext) -> list[str]:
    """Known scalar calls the source advertises, including literal-only calls."""
    advertised = {fn for functions in schema.capabilities.functions.values() for fn in functions}
    return [fn for fn in SCALAR_FUNCTIONS if fn in advertised]


def _scalar_detail(fn: str) -> str:
    spec = SCALAR_FUNCTIONS[fn]
    args = [kind if index < spec.minimum else f"[{kind}]" for index, kind in enumerate(spec.parameters)]
    if spec.variadic:
        args.append("…")
    return f"{fn}({', '.join(args)}) → {spec.result}"




def _numeric_type(type_: str) -> bool:
    type_ = type_.removeprefix("!")
    return type_.startswith(("int", "uint", "float", "decimal")) or type_ in {"null", "unknown"}


def _select_entries(
    session: Session, doc: str, token: str, schema: SchemaContext, *, arithmetic: bool = False,
) -> list[Entry]:
    """`\\select` offers fields first, then the functions that apply to them.

    An open call is handled one level up, where its argument's span is known;
    here the token is the whole expression segment being typed.
    """
    columns = session.column_entries(doc, schema=schema, qualified="." in token)
    rows = [
        (0, name, detail, f"{name} ") for name, type_, detail in columns
        if not arithmetic or _numeric_type(type_)
    ]
    rows += [(1, fn, "aggregate", f"{fn}(") for fn in _aggregate_fns(schema)]
    if not arithmetic:
        rows += [(2, fn, "temporal part", f"{fn}(") for fn in _temporal_fns(columns, schema)]
        rows += [(3, fn, "window rank", f"{fn}() over (") for fn in _rank_fns(schema)]
    rows += [
        (4, fn, _scalar_detail(fn), f"{fn}(") for fn in _scalar_fns(schema)
        if not arithmetic or SCALAR_FUNCTIONS[fn].result in {"integer", "float", "any"}
    ]
    return _history_offers(session, doc, rows, token)


def _call_argument_entries(
    session: Session, doc: str, fn: str, partial: str, argument: int = 0,
    *, schema: SchemaContext, arithmetic: bool = False, close: bool = True,
    numeric_result: bool = False,
    result_kind: ArgumentKind | None = None,
) -> list[Entry]:
    """Complete the current argument without replacing its enclosing call."""
    columns = session.column_entries(doc, schema=schema, qualified="." in partial)
    if fn in SCALAR_FUNCTIONS:
        if fn not in _scalar_fns(schema):
            return []
        spec = SCALAR_FUNCTIONS[fn]
        arithmetic = arithmetic or (numeric_result and spec.result == "any")
        kind = spec.argument_kind(argument)
        if spec.result_argument == argument and result_kind is not None:
            kind = result_kind
        if kind is None or (arithmetic and kind == "string"):
            return []
        # Multi-argument calls stay open: the user chooses additional optional
        # or variadic arguments. Unary calls complete like existing date parts.
        suffix = ")" if len(spec.parameters) == 1 else ", " if argument + 1 < spec.minimum else ""
        if not close:
            suffix = ""
        rows = [
            (0, name, detail, f"{name}{suffix}")
            for name, type_, detail in columns
            if accepts_type(kind, type_) and (not arithmetic or _numeric_type(type_))
        ]
        rows += [
            (1, nested, _scalar_detail(nested), f"{nested}(")
            for nested in _scalar_fns(schema)
            if SCALAR_FUNCTIONS[nested].accepts_result(kind)
            and (not arithmetic or SCALAR_FUNCTIONS[nested].result in {"integer", "float", "any"})
        ]
        return _history_offers(session, doc, rows, partial)
    if argument or arithmetic:
        return []
    if fn in _aggregate_fns(schema):
        suffix = ")" if close else ""
        rows = [
            (0, name, detail, f"{name}{suffix}") for name, type_, detail in columns
            if not numeric_result or fn == "count" or _numeric_type(type_)
        ]
        if fn == "count":
            rows.append((1, "*", "every row", f"*{suffix}"))
        return _history_offers(session, doc, rows, partial)
    families = {family for family, fns in schema.capabilities.functions.items() if fn in fns}
    if fn not in TEMPORAL or not families:
        return []
    rows = [
        (0, name, detail, f"{name})")
        for name, type_, detail in columns
        if dtype_family(type_) in families
    ]
    return _history_offers(session, doc, rows, partial)


def _value_text(value: str) -> str:
    """A value as `\\where` wants it: bare when numeric, quoted otherwise."""
    return value if looks_numeric(value) else f'"{value}"'


def _value_entries(
    session: Session, doc: str, column: str, token: str, schema: SchemaContext,
    value_lookup: Callable[[str], list[str]] | None = None,
) -> list[Entry]:
    """That column's distinct values, searched by what is being typed."""
    rows = [
        (0, value, f"{column} value", f"{_value_text(value)} ")
        for value in (value_lookup(column) if value_lookup is not None else session.values_for(doc, column, schema=schema))
    ]
    dataset = session.dataset_of(doc, column, schema=schema)
    target = f"{dataset}.{column.rsplit('.', 1)[-1]}"
    recency = {value: rank for (kind, owner, value), rank in session.completion_usage().items()
               if kind == "value" and owner == target}
    return _offers(rows, token.strip("\"'"), recency=recency)[:VALUE_SUGGESTIONS]


def _fn_call_entries(session: Session, token: str, schema: SchemaContext) -> list[Entry]:
    """Saved functions as call rows for a dataset-taking clause.

    Each inserts `name()` with the caret between the parens, so accepting
    `\from mont…` lands at `\from monthly(|)` ready for the argument.
    """
    kept = [
        (rank, index, call, detail)
        for index, (call, detail) in enumerate(session.fn_call_rows(schema=schema))
        if (rank := match_rank(call, token)) >= 0
    ]
    recency = session.completion_ranks("", category="function")
    kept.sort(key=lambda row: (row[0], recency.get(row[2].split("(", 1)[0], float("inf"))))
    return [Entry(label=call, insert=call, detail=detail, cursor_back=1) for _, _, call, detail in kept]


def _fn_view(schema: SchemaContext, token: str, slash: int) -> View | None:
    """Complete saved calls; offer creation after matching functions.

    The Functions action opens the library for editing. Saved rows here are
    ordinary source-clause completions, never editor-navigation actions.
    """
    entries: list[Entry] = []
    for fn in schema.fns:
        rank = match_rank(fn.name, token)
        if rank < 0:
            continue
        detail = f"{', '.join(fn.params)}" if fn.params else "no arguments"
        if fn.doc:
            detail = f"{detail} · {fn.doc}"
        entries.append(Entry(label=fn.name, insert=f"\\from {fn.name}()",
                             detail=detail, cursor_back=1))
    if not token or is_identifier(token):
        exact = any(entry.label.lower() == token.lower() for entry in entries)
        if not exact:
            label = token if token else "New function…"
            detail = f"create \\fn {token}" if token else "define a new table-valued function"
            entries.append(Entry(label=label, detail=detail, action=f"fn-new:{token}"))
    return View(start=slash, token=token, entries=entries) if entries else None


def _parameter_token(before: str) -> tuple[int, str] | None:
    """Find a parameter ending at the caret, including an unfinished bare `@`."""
    # The parser requires a name; a temporary identifier lets its quote and
    # word-boundary rules also decide whether a bare `@` starts a parameter.
    probe = before + "_" if before.endswith("@") else before
    spans = param_spans(probe)
    if spans and spans[-1][1] == len(probe):
        start = spans[-1][0]
        return start, before[start:]
    return None


def _join_view(
    session: Session, doc: str, line: str, column: int, rest: str, start: int,
    *, row: int | None = None,
) -> View | None:
    """Offer entire declared constraints, qualified with this join's identifiers."""
    on = keyword_positions(rest, "on")
    head = rest[:on[-1]] if on else rest
    source = parse_source(head.strip())
    if source is None or source[1] is not None:
        return None
    if not on and not rest[-1:].isspace():
        return None
    dataset, _, alias = source
    identifier = alias or dataset
    right = session.schema.table_by_name(dataset)
    if right is None:
        return None
    lines = doc.split("\n")
    if row is None:
        row = next((index for index in range(len(lines) - 1, -1, -1) if lines[index] == line), len(lines))
    opened = session.open_tables("\n".join(lines[:row]))
    prefix = "" if on else "on "
    on_end = on[-1] + 2 if on else len(rest)
    token = rest[on_end:].lstrip() if on else ""
    offset = len(rest) - len(token) if on else len(rest)
    entries: list[Entry] = []
    seen: set[str] = set()
    for left in opened:
        table = session.schema.table_by_name(left.dataset)
        if table is None:
            continue
        constraints = [
            (fk.columns, fk.target_columns) for fk in table.foreign_keys if fk.target_table == right.name
        ] + [
            (fk.target_columns, fk.columns) for fk in right.foreign_keys if fk.target_table == table.name
        ]
        for left_columns, right_columns in constraints:
            if (not left_columns or len(left_columns) != len(right_columns)
                    or not all(any(column.name == name for column in table.columns) for name in left_columns)
                    or not all(any(column.name == name for column in right.columns) for name in right_columns)):
                continue
            predicate = " and ".join(
                f"{left.identifier}.{a} = {identifier}.{b}" for a, b in zip(left_columns, right_columns)
            )
            if predicate in seen or match_rank(predicate, token) < 0:
                continue
            seen.add(predicate)
            entries.append(Entry(prefix + predicate, prefix + predicate + " ", "foreign key"))
    entries.sort(key=lambda entry: match_rank(entry.label.removeprefix("on "), token))
    return View(start + offset, token, entries, phase="argument", end=len(line)) if entries else None


def _default_group_columns(doc: str, schema: SchemaContext) -> str:
    """Selected source columns in order, without replacing an explicit group."""
    ast = parse_query(doc, schema=schema)
    if ast.group_by or any(item.star or item.window or item.subquery for item in ast.select):
        return ""
    # Computed projections already group by their expression in the engine.
    # Their input columns would change the grouping; output aliases are not
    # source-column references accepted by the group clause.
    return ", ".join(dict.fromkeys(item.column for item in ast.select if item.column))


def view_for(
    session: Session, doc: str, line: str, column: int, *, parameters: Iterable[str] = (),
    value_lookup: Callable[[str], list[str]] | None = None, row: int | None = None,
) -> View | None:
    """The palette view for the line text left of the caret, or `None`.

    `None` means "no palette": no `\\` before the caret, a command the palette
    has nothing to say about, or nothing left to suggest for what is typed.
    """
    schema = session.schema
    if row is not None:
        lines = doc.split("\n")
        begin, stop = block_bounds(lines, row + 1)
        doc = "\n".join(lines[begin:stop])
        row -= begin
    before = line[:column]
    parameter = _parameter_token(before) if parameters else None
    if parameter is not None:
        start, token = parameter
        rows = ((0, name, "parameter", f"@{name}") for name in parameters)
        entries = [
            Entry(label=entry.insert, insert=entry.insert, detail=entry.detail)
            for entry in _offers(rows, token[1:])
        ]
        end = next((end for begin, end, _ in param_spans(line) if begin == start), column)
        return View(start, line[start:end], entries, phase="argument", end=end) if entries else None
    slash, inline_filter = _command_context(before)
    if slash < 0:
        return None
    word, gap, rest = _split(before[slash + 1 :])
    if word.lower() == FN_COMMAND:
        # Saved functions complete as calls; only the explicit create row
        # opens an editor. Replace the entire `\fn ...` span on acceptance.
        return _fn_view(schema, rest.strip(), slash)
    if inline_filter and not gap:
        entries = [
            Entry(label="\\where", insert="\\where ", detail="filter this aggregate's rows")
        ] if "where".startswith(word.lower()) else []
        return View(slash, word, entries, phase="argument", end=_token_end(line, slash, column)) if entries else None
    if not gap:
        if "(" in word:
            return None  # The name is complete; leave typed arguments untouched.
        entries = _command_entries(session, word, schema)
        end = _token_end(line, slash, column)
        if not line[end:].strip():
            for index, entry in enumerate(entries):
                if entry.label == "\\group":
                    group_doc = doc
                    if row is not None and line[:slash].strip():
                        # The accepted command will move to its own line. Do not
                        # let its unfinished text corrupt the preceding select.
                        block_lines = doc.split("\n")
                        block_lines[row] = line[:slash] + line[end:]
                        group_doc = "\n".join(block_lines)
                    columns = _default_group_columns(group_doc, schema)
                    if columns:
                        entries[index] = Entry("\\group", f"\\group {columns} ", f"group selected columns · {columns}")
                    break
        return View(start=slash, token=word, entries=entries, end=end) if entries else None

    command = word.lower()
    if command == "join":
        joined = _join_view(session, doc, line, column, rest, slash + 1 + len(word) + len(gap), row=row)
        if joined is not None:
            return joined
    arguments = DATASET_COMMANDS | SET_OP_COMMANDS | {"group", "where", "drop"}
    if command in arguments:
        token, offset = (_where_token(rest) if command != "drop" else _word(rest))
    elif command == "select":
        position = _expression_position(rest)
        if position is None:
            return None
        offset, fn, argument = position.offset, position.fn, position.argument
        token = rest[offset:].strip()
    else:
        segment, offset = _segment(rest)
        token = segment.strip()
    start = slash + 1 + len(word) + len(gap) + offset
    end = _token_end(line, start, column, relation=(
        command in DATASET_COMMANDS | SET_OP_COMMANDS
        or relation_parts(token.rpartition(".")[0]) is not None
    ))

    if command == "temp":
        # A temp table's name is new text, not a choice — nothing to offer.
        return None
    if command == "drop":
        rows = [(0, name, "temp table", f"{name} ") for name in session.temp_tables()]
        entries = _offers(rows, token)
    elif command in DATASET_COMMANDS or command in SET_OP_COMMANDS:
        head = rest[:offset].split()
        # `\union all |` still takes a name: the modifier is not a table.
        modifier = head[0].lower() if head and head[0].lower() in SET_OP_MODIFIER_WORDS else ""
        head = head[1:] if modifier else head
        if head:
            # `\from events |` is done: a table is already named there, and the
            # rest of the clause (`as alias`, `on col`) takes no dataset — so a
            # stray Enter cannot append a second one.
            return None
        rows = [
            (0, name, detail, session.table_insert(doc, name, schema=schema)
             if command in DATASET_COMMANDS and not line[end:].strip() else f"{name} ")
            for name, detail in session.dataset_entries(doc, schema=schema)
        ]
        if command in SET_OP_COMMANDS and not modifier:
            # The modifier leads the argument, and only once.
            rows += [(1, name, detail, f"{name} ") for name, detail in SET_OP_MODIFIERS]
        # Saved functions complete here too — accepted as a `name()` call with
        # the caret inside the parens — after the datasets, in match order.
        entries = _history_offers(session, doc, rows, token, category="dataset") + _fn_call_entries(session, token, schema)
    elif command == "select":
        if fn is not None:
            entries = _call_argument_entries(
                session, doc, fn, token, argument, schema=schema,
                arithmetic=position.arithmetic,
                close=position.close and not line[end:].lstrip().startswith((")", ",", "\\where", *ARITHMETIC_PRECEDENCE)),
                numeric_result=position.numeric_result,
                result_kind=position.result_kind,
            )
        else:
            entries = _select_entries(session, doc, token, schema, arithmetic=position.arithmetic)
    elif command in COLUMN_COMMANDS:
        entries = _column_offers(session, doc, token, schema)
        if command == "group" and not rest.strip() and not line[column:].strip():
            columns = _default_group_columns(doc, schema)
            if columns:
                entries = [Entry(columns, f"{columns} ", "all selected non-aggregate columns"),
                           *(entry for entry in entries if entry.label != columns)]
        elif command == "group" and rest.strip() and not token and not rest.rstrip().endswith(","):
            return None  # A completed group lets Enter start the next clause.
    elif command == "where":
        entries = _where_entries(session, doc, rest[:offset], token, schema, value_lookup)
    else:
        return None
    return View(start=start, token=token, entries=entries, phase="argument",
                end=end) if entries else None


def _where_entries(
    session: Session, doc: str, before: str, token: str, schema: SchemaContext,
    value_lookup: Callable[[str], list[str]] | None = None,
) -> list[Entry]:
    """`\\where` completion over a condition tree of `and`/`or`.

    The raw text before the caret is read as far as it goes, masking quotes,
    parens, and `between`'s own `and` (the same `split_logic` the parser uses,
    so a bound never reads as a joiner). Each state offers the next thing the
    grammar wants: a column after a joiner or at the start; operators after a
    column; then the operator's operand — values for `=`/`like`/`between`
    bounds, nothing typed for a regex pattern, an `in`-list, or a subquery —
    and `and`/`or` after a complete condition.
    """
    ops = operators(schema)
    piece = before
    for _ in range(MAX_EXPRESSION_DEPTH + 1):
        piece = split_logic(split_logic(piece, "or")[-1], "and")[-1].strip()
        if not piece.startswith("("):
            break
        if take_paren(piece) is not None:
            return _joiners(token)
        # Descend and re-split at every open group, not only the outermost one.
        piece = piece[1:]
    else:
        return []
    words = piece.split()
    if not words:
        # Either nothing is typed yet, or a joining `and`/`or` just landed
        # (its keyword is the only reason the last piece is blank): the next
        # thing a condition wants is a column.
        return _column_offers(session, doc, token, schema)
    head = where_head(piece.strip())
    if head is None:
        if _COL_PATTER.fullmatch(piece.strip()):
            return _offers([(0, op, "operator", f"{op} ") for op in ops], token)
        # A column plus partial operator words (`amount is`, `amount n`): the
        # multi-word operators those words begin, offered as the remainder of
        # the operator so accepting it completes the phrase in place.
        typed = " ".join(piece.split()[1:]).lower()
        rows = [
            (0, op, "operator", f"{op[len(typed):].lstrip()} ")
            for op in ops
            if op.lower().startswith(typed) and op.lower() != typed
        ]
        return _offers(rows, token)
    column, op, tail = head
    if op in NULL_OPS:
        # `is [not] null` needs no operand: the condition is complete.
        return _joiners(token)
    if op == "between":
        return _between_entries(session, doc, column, tail, token, schema, value_lookup)
    if op in REGEX_OPS:
        # A regex pattern is typed, never offered from the value pool.
        return _joiners(token) if tail and _closed_operand(tail) else []
    if op in SUBQUERY_OPS:
        # An `in` takes a `( … )` — a list or a subquery — which is typed.
        return _joiners(token) if tail.endswith(")") else []
    if not tail:
        return _value_entries(session, doc, column, token, schema, value_lookup)
    if _closed_operand(tail):
        return _joiners(token)
    if tail.startswith(('"', "'")):
        return _value_entries(session, doc, column, token, schema, value_lookup)
    return []


def _joiners(token: str) -> list[Entry]:
    """`and`/`or`: the rows that follow any complete condition."""
    return _offers(
        [(0, joiner, "join conditions", f"{joiner} ") for joiner in _JOINERS],
        token,
    )


def _between_entries(
    session: Session, doc: str, col: str, tail: str, token: str, schema: SchemaContext,
    value_lookup: Callable[[str], list[str]] | None = None,
) -> list[Entry]:
    """`between`'s low, its `and`, then its high — each an offer in turn."""
    bounds = [bound.strip() for bound in split_logic(tail, "and")]
    if len(bounds) == 1:
        if not bounds[0] or not _closed_operand(bounds[0]):
            return _value_entries(session, doc, col, token, schema, value_lookup)
        return _offers([(0, "and", "between's high bound", "and ")], token)
    high = bounds[1]
    if not high or not _closed_operand(high):
        return _value_entries(session, doc, col, token, schema, value_lookup)
    return _joiners(token)


def _closed_operand(text: str) -> bool:
    """True when an operand is finished: a complete number or a closed quote."""
    if _NUMBER_RE.match(text):
        return True
    return len(text) >= 2 and text[0] in "\"'" and text[-1] == text[0]



class CommandPalette(OptionList):
    """The overlay itself: an option list that is driven, never focused."""

    can_focus = False

    class ActionPerformed(Message):
        """A palette action (`Run`, `Compile`, …) for the app to carry out."""

        def __init__(self, action: str) -> None:
            self.action = action
            super().__init__()
    class ValuesRequested(Message):
        """An on-demand read; the app owns connection sharing and background work."""

        def __init__(self, palette: CommandPalette, source: DataSource, dataset: str, column: str,
                     key: tuple[int, str, str, int]) -> None:
            super().__init__()
            self.palette = palette
            self.source = source
            self.dataset = dataset
            self.column = column
            self.key = key


    def __init__(
        self, session: Session, *, source_id: str | None = None,
        workspace_actions: bool = True, parameters: Callable[[], Iterable[str]] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.session = session
        self.source_id = source_id
        self.workspace_actions = workspace_actions
        self.parameters = parameters
        self.editor = None
        self._view: View | None = None
        self._preferred_width = 24
        # True after Escape: the offers stay shut until a `\` (or Escape) lifts it.
        self._dismissed = False
        self._value_key: tuple[int, str, str, int] | None = None
        self._value_result: list[str] | None = None

    @property
    def is_open(self) -> bool:
        return self.has_class("open")

    @property
    def view(self) -> View | None:
        """The view currently displayed (what an accept applies to)."""
        return self._view

    def attach(self, editor) -> None:
        """Bind this palette to the editor whose text and caret it follows."""
        self.editor = editor

    def on_mouse_down(self, event: events.MouseDown) -> None:
        # Textual otherwise focuses a scrollable ancestor of this unfocusable
        # popup, dismissing it before the click can select a row.
        if self.editor is not None:
            self.editor.focus()
        event.stop()

    def reset_values(self) -> None:
        """Drop the current transient response after cache settings/clear change."""
        self._value_key = None
        self._value_result = None
        self.close()

    def needs_values(self, key: tuple[int, str, str, int]) -> bool:
        return (self.is_mounted and self._value_key == key and self._value_result is None
                and key[-1] == self.session.value_cache_epoch
                and self.session.intellisense and not self._dismissed)

    def values_received(self, key: tuple[int, str, str, int], values: list[object]) -> None:
        if not self.needs_values(key):
            return
        self._value_result = [str(value) for value in values if value is not None]
        self.sync(respect_dismissal=True)

    # -- opening / closing --------------------------------------------------

    def sync(self, *, respect_dismissal: bool = False) -> None:
        """Follow the editor's caret: offer what belongs there, or close.

        The offers are on wherever the caret lands somewhere the language has
        something to say — typed to, inserted into from the schema tree, or
        accepted into. An accept is not an exit: it leaves the palette exactly
        where the same characters typed by hand would, so `\\sel`, `sum(`, and
        the column after them keep offering without a second summon.

        Two things silence the popup: Escape's dismissal (which holds while the
        user keeps typing, and a fresh `\\` — or Escape again — lifts) and the
        Settings menu's Intellisense switch, the hard off: nothing opens while
        it is off, and `ctrl+comma` is the way back to Settings.
        """
        editor = self.editor
        if self.session.busy or (respect_dismissal and self._dismissed):
            self.close()
            return
        if editor is None:
            return
        if editor.read_only or editor.selection.start != editor.selection.end:
            self.close()
            return
        row, column = editor.cursor_location
        line = editor.document[row]
        if column > 0 and (
            line[column - 1] == "\\"
            or (self.parameters is not None and line[column - 1] == "@" and _parameter_token(line[:column]))
        ):
            # An explicit `\` or function parameter `@` lifts Escape's dismissal.
            self._dismissed = False
        request: CommandPalette.ValuesRequested | None = None
        value_key: tuple[int, str, str, int] | None = None
        value_result: list[str] | None = None

        def value_lookup(column_name: str) -> list[str]:
            nonlocal request, value_key, value_result
            source = self.session.source
            dataset = self.session.dataset_of(editor.text, column_name)
            if dataset is None:
                return []
            name = column_name.rsplit(".", 1)[-1]
            value_key = (id(source), dataset, name, self.session.value_cache_epoch)
            if value_key == self._value_key and self._value_result is not None:
                value_result = self._value_result
                return value_result
            cached = self.session.cached_values(source, dataset, name)
            if cached is not None:
                value_result = [str(value) for value in cached[0] if value is not None]
                return value_result
            if value_key != self._value_key:
                request = self.ValuesRequested(self, source, dataset, name, value_key)
            return []

        with self.session.target_source(self.source_id):
            view = (
                view_for(
                    self.session, editor.text, line, column,
                    parameters=self.parameters() if self.parameters is not None else (),
                    row=row,
                    value_lookup=value_lookup,
                )
                if self.session.intellisense and not self._dismissed
                else None
            )
        if value_key != self._value_key or value_result is not None:
            self._value_key = value_key
            self._value_result = value_result
        if request is not None:
            self.post_message(request.set_sender(self))
        if view is not None and not self.workspace_actions:
            view = View(view.start, view.token, [e for e in view.entries if not e.action], view.phase, view.end)
            if not view.entries:
                view = None
        if (view is not None and view.phase == "argument"
                and (len(view.entries) == 1 or line[:column].endswith((" ", "\t")))
                and (view.end is None or view.end == column)
                and any(not entry.cursor_back and not entry.insert.startswith("\\")
                        and view.highlighted_is_typed(index)
                        for index, entry in enumerate(view.entries))):
            # A completed leaf has no next choice. Keep calls, table aliases,
            # operators and other genuine continuations, not the accepted field.
            view = None
        if view is None:
            self.close()
            return
        previous = self._view
        if previous is None or view.entries != previous.entries:
            self.clear_options()
            prompts = [prompt(entry.label, entry.detail) for entry in view.entries]
            self._preferred_width = min(62, max(24, max(item.cell_length for item in prompts) + 5))
            self.add_options([Option(item, id=f"opt-{index}") for index, item in enumerate(prompts)])
            self.highlighted = 0
        elif self.highlighted is None:
            self.highlighted = 0
        self._view = view
        placed = self._place()
        self.styles.visibility = "visible" if placed else "hidden"
        if not self.is_open:
            self.add_class("open")
            self.scroll_to_highlight()
        if not placed:
            # A just-shown editor has no usable geometry until layout finishes.
            # Keep its current offers, but never paint them at an old position.
            self.call_after_refresh(self.reposition)

    def _place(self) -> bool:
        """Fit up to six single-line offers beside the actual rendered caret."""
        pane = self.parent
        editor = self.editor
        if editor is None or pane is None:
            return False
        visible = editor.content_region.intersection(pane.content_region).intersection(self.screen.region)
        caret = editor.cursor_screen_offset
        if caret not in visible:
            return False
        desired_height = min(self.option_count, 6) + 2
        below = visible.bottom - caret.y - 1
        above = caret.y - visible.y
        place_below = below >= desired_height or below >= above
        height = min(desired_height, below if place_below else above)
        if height < 3 or visible.width < 8:
            return False
        width = min(self._preferred_width, visible.width)
        left = max(visible.x, min(caret.x, visible.right - width))
        top = caret.y + 1 if place_below else caret.y - height
        self.styles.offset = (left - pane.content_region.x, top - pane.content_region.y)
        self.styles.width = width
        self.styles.height = height
        return True

    def reposition(self) -> None:
        """Follow wrapping, scrolling and resizing without reopening a dismissed list."""
        if self.is_open:
            if self._place():
                self.styles.visibility = "visible"
            else:
                self.close()

    def close(self) -> None:
        """Hide the palette without touching the document."""
        self._view = None
        self.remove_class("open")

    def escape(self) -> None:
        """Escape shows and hides the popup — the same key, both ways.

        Open (or offering something), it closes and stays shut while the user
        keeps typing; closed, it lifts that dismissal and offers again.
        """
        if self.is_open or (self._value_key is not None and self._value_result is None and not self._dismissed):
            self.close()
            self._dismissed = True
            return
        self._dismissed = False
        self._value_key = None
        self._value_result = None
        self.sync()

    def move(self, delta: int) -> None:
        """Move the highlight, wrapping at both ends."""
        if not self.option_count:
            return
        if self.highlighted is None:
            self.highlighted = 0
        else:
            self.highlighted = (self.highlighted + delta) % self.option_count
        self.scroll_to_highlight()

    # -- accepting ----------------------------------------------------------

    def accept_highlighted(self, *, newline: bool = False) -> bool:
        """Accept the highlighted row; False when there is nothing to accept."""
        if not self.is_open:
            self.sync(respect_dismissal=True)
        index = self.highlighted
        return False if index is None else self.accept(index, newline=newline)

    def accept(self, index: int, *, newline: bool = False) -> bool:
        """Apply row `index`; False when accepting it would change nothing.

        The palette is not closed on the way out: the accept ends by syncing to
        the caret it just moved, so the offers at the landing spot are already
        there — `\\sel` completes to `\\select ` with the fields the new clause
        takes, and `sum(` keeps offering its argument.
        """
        self.sync(respect_dismissal=True)
        view = self._view
        editor = self.editor
        if (view is None or editor is None or editor.read_only
                or editor.selection.start != editor.selection.end
                or not 0 <= index < len(view.entries)):
            return False
        entry = view.entries[index]
        row, column = editor.cursor_location
        if newline and view.phase == "argument" and entry.label == view.token.strip():
            before = editor.document[row][:column]
            slash, _ = _command_context(before)
            word, _, _ = _split(before[slash + 1:])
            if word.lower() in DATASET_COMMANDS:
                # A fully typed table followed by Enter is a newline, not an
                # implicit completion choice. Tab/click still select an alias.
                return False
        if (not entry.action and view.highlighted_is_typed(index)
                and (newline or not entry.insert.startswith("\\"))
                and (view.end is None or view.end == column)):
            return False
        if entry.action:
            editor.replace("", (row, view.start), (row, column))
            editor.cursor_location = (row, view.start)
            self.post_message(self.ActionPerformed(entry.action))
        else:
            command = entry.insert.strip()
            if command.startswith("\\") and view.phase == "command":
                name = entry.label[1:].lower()
                # Saved call offers are source clauses, just like \from.
                self._take_clause(editor, row, column, view,
                                  name if name in CLAUSE_NAMES else "from", entry)
            else:
                end = column if view.end is None else view.end
                insert = entry.insert
                after = editor.document[row][end:]
                if after and (after[0].isspace() or after[0] in "),"):
                    insert = insert.rstrip()
                editor.replace(insert, (row, view.start), (row, end))
                editor.cursor_location = (row, view.start + len(insert) - entry.cursor_back)
        self.sync()
        return True

    def _take_clause(self, editor, row: int, column: int, view: View, name: str, entry: Entry) -> None:
        """A clause command takes its own line — or the line its clause has.

        The lookup runs in the block the caret is editing (`clause_line`), so a
        `\\where` under a `\\with` and the document's own `\\where` are different
        clauses. Finding one takes the caret to it and writes nothing; otherwise
        the command breaks the line first, so it never lands after whatever the
        caret happened to be sitting in.
        """
        end = column if view.end is None else view.end
        target = clause_line(editor.text, row + 1, name)
        if target is not None and target != row + 1:
            editor.replace("", (row, view.start), (row, end))
            editor.cursor_location = (target - 1, len(editor.document[target - 1]))
            return
        before = editor.document[row][: view.start]
        after = editor.document[row][end:]
        indent = before[: len(before) - len(before.lstrip())]
        prefix = f"\n{indent}" if before.strip() else ""
        suffix = f"\n{indent}" if before.strip() and after.strip() else ""
        command = entry.insert
        if not suffix and after and after[0].isspace():
            command = command.rstrip()
        editor.replace(f"{prefix}{command}{suffix}", (row, view.start), (row, end))
        line, start = (row + 1, len(indent)) if prefix else (row, view.start)
        editor.cursor_location = (line, start + len(command) - entry.cursor_back)


class EditorPane(Vertical):
    """The document editor and its palette, and the owner of the palette's keys.

    They are claimed here rather than on the editor itself for two reasons: a
    TextArea's own key handler cannot be suppressed from a subclass (Textual
    dispatches every `_on_key` in the MRO), and here they stay scoped — only
    while the focus is inside this pane, so Enter keeps selecting rows in the
    results table and every other widget keeps its own keys.
    """

    BINDINGS = [
        Binding("up", "palette_up", "Cursor up", show=False, priority=True),
        Binding("down", "palette_down", "Cursor down", show=False, priority=True),
        Binding("enter", "palette_enter", "Newline", show=False, priority=True),
        Binding("escape", "palette_escape", "Close the palette", show=False, priority=True),
        # TextArea binds `home,ctrl+a` to "cursor line start"; in an editor the
        # user means select all, and the pane claims the key to say so.
        Binding("ctrl+a", "select_all", "Select all", show=False, priority=True),
        Binding("tab", "palette_tab", "Accept suggestion", show=False, priority=True),
    ]

    @property
    def editor(self) -> TextArea:
        """The document editor."""
        return self.query_one(TextArea)

    @property
    def palette(self) -> CommandPalette:
        """The `\\` overlay."""
        return self.query_one(CommandPalette)

    def on_mount(self) -> None:
        self.palette.attach(self.editor)
        self.watch(self.editor, "scroll_x", self._queue_palette_placement, init=False)
        self.watch(self.editor, "scroll_y", self._queue_palette_placement, init=False)

    def _queue_palette_placement(self) -> None:
        self.call_after_refresh(self.palette.reposition)

    def on_resize(self) -> None:
        self.call_after_refresh(self._resize_palette)

    def _resize_palette(self) -> None:
        if self.app.focused is self.editor:
            self.editor.scroll_cursor_visible()
        self.palette.reposition()

    def on_descendant_blur(self, event: events.DescendantBlur) -> None:
        if event.widget is self.editor:
            self.call_after_refresh(self._close_unfocused_palette)

    def _close_unfocused_palette(self) -> None:
        if self.app.focused is not self.editor:
            self.palette.close()

    @on(TextArea.Changed)
    def _editor_changed(self) -> None:
        self.palette.sync()

    @on(TextArea.SelectionChanged)
    def _editor_caret_moved(self) -> None:
        if self.palette.is_open:
            self.palette.sync()

    @on(OptionList.OptionSelected)
    def _palette_clicked(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.palette.accept(event.option_index)
        self.editor.focus()

    def action_palette_up(self) -> None:
        """Move the palette's highlight, or the caret when it is closed."""
        if self.palette.is_open:
            self.palette.move(-1)
        else:
            self.editor.action_cursor_up()

    def action_palette_down(self) -> None:
        """Move the palette's highlight, or the caret when it is closed."""
        if self.palette.is_open:
            self.palette.move(1)
        else:
            self.editor.action_cursor_down()

    def action_palette_enter(self) -> None:
        """Accept the highlighted suggestion, or insert a newline.

        An accept that would change nothing is not an accept: the palette
        closes and Enter means what it always means.
        """
        palette = self.palette
        if palette.is_open and palette.accept_highlighted(newline=True):
            return
        palette.close()
        editor = self.editor
        if editor.read_only:
            return
        start, end = editor.selection
        editor.replace("\n", start, end, maintain_selection_offset=False)

    def action_palette_escape(self) -> None:
        """Escape shows and hides the popup; it does nothing else to the document."""
        self.palette.escape()

    def action_select_all(self) -> None:
        """`ctrl+a` selects the whole document (TextArea would go to line start)."""
        self.editor.select_all()

    def action_palette_tab(self) -> bool:
        """Accept an offer without moving focus; report whether it changed the document."""
        return self.palette.accept_highlighted()
