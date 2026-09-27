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

from textual import on
from textual.binding import Binding
from textual.containers import Vertical
from textual.content import Content
from textual.message import Message
from textual.widgets import OptionList, TextArea
from textual.widgets.option_list import Option

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
    split_logic,
    take_paren,
    where_head,
)
from d8r.query.functions import SCALAR_FUNCTIONS, ArgumentKind

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

# The function-library summon. Like the pane toggles it is an action, not a
# clause: `\fn` never becomes document text.
FN_COMMAND = "fn"

# Fallback comparison operators; the live capability set is preferred.
DEFAULT_OPERATORS: tuple[str, ...] = (
    "=", "!=", ">", ">=", "<", "<=", "like", "ilike", "in", "not in",
    "between", "is null", "is not null",
)

# The words that join two conditions; `between`'s own `and` is not one of them
# (the parser's `split_logic` masks it), so a bound never reads as a joiner.
_JOINERS: tuple[str, ...] = ("and", "or")
_COL_PATTER = re.compile(rf"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?$")
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
    # Parameter completion replaces the full token even when the caret is inside it.
    end: int | None = None

    @property
    def labels(self) -> list[str]:
        return [entry.label for entry in self.entries]

    def highlighted_is_typed(self, index: int) -> bool:
        """True when the highlighted suggestion is already what the user typed.

        Accepting it would be a no-op, so the editor lets the key through
        instead of swallowing it (Enter then means a newline, as it should).
        """
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


def _offers(rows: Iterable[tuple[int, str, str, str]], token: str) -> list[Entry]:
    """(group, name, detail, insert) rows matching `token`, group then rank first.

    The group is the block a row belongs to (fields before functions); within a
    block the best match leads and equal ranks keep the order they were built in.
    """
    kept = [
        (group, rank, name, detail, insert)
        for group, name, detail, insert in rows
        if (rank := match_rank(name, token)) >= 0
    ]
    kept.sort(key=lambda row: (row[0], row[1]))
    return [Entry(label=name, insert=insert, detail=detail) for _, _, name, detail, insert in kept]


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


def _command_entries(token: str, schema: SchemaContext) -> list[Entry]:
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
    best = [row for row in (*commands, *actions) if row[0] >= 0]
    best.sort(key=lambda row: (row[0], row[1]))
    return [entry for _, _, entry in best]


def _segment(rest: str) -> tuple[str, int]:
    """The comma-separated argument segment being typed, and its offset."""
    offset = rest.rfind(",") + 1
    raw = rest[offset:]
    return raw.lstrip(), offset + (len(raw) - len(raw.lstrip()))


def _expression_position(text: str) -> tuple[int, str | None, int] | None:
    """Locate the current select expression or innermost call argument.

    Commas in strings and completed nested calls never restart completion.
    Inside a quoted literal there is nothing for the palette to insert.
    """
    offset = 0
    stack: list[tuple[str, int, int]] = []
    quote: str | None = None
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "(":
            match = _CALL_NAME_RE.search(text[:index])
            stack.append((match.group(1).lower() if match else "", 0, index + 1))
        elif char == ")":
            if stack:
                stack.pop()
        elif char == ",":
            if stack:
                fn, argument, _ = stack[-1]
                stack[-1] = (fn, argument + 1, index + 1)
            else:
                offset = index + 1
    if quote:
        return None
    fn, argument, offset = stack[-1] if stack else (None, 0, offset)
    while offset < len(text) and text[offset].isspace():
        offset += 1
    return offset, fn, argument


def _word(rest: str) -> tuple[str, int]:
    """The word the caret is in (or right after) and its offset in `rest`.

    `\\group` and `\\where` take whitespace-separated arguments, so what is
    being completed there is the last word — not the whole comma segment.
    """
    if not rest or rest[-1].isspace():
        return "", len(rest)
    token = rest.split()[-1]
    return token, rest.rfind(token)


def _group_token(rest: str, token: str, offset: int) -> tuple[str, int]:
    """The `\where` token with group parens peeled off its ends.

    Parens are grammar, not filter text: an opening `(` must not filter the
    column offers it sits before, and a closing `)` ends the *previous*
    condition, so after it nothing is being typed — the state machine reads
    the closed group from `before` and offers the joiners.
    """
    while token.startswith("("):
        token = token[1:]
        offset += 1
    if token.endswith(")"):
        return "", len(rest)
    return token, offset


def _column_offers(session: Session, doc: str, token: str, schema: SchemaContext) -> list[Entry]:
    """The document's open-table columns as palette rows."""
    rows = [
        (0, name, detail, f"{name} ")
        for name, _, detail in session.column_entries(doc, schema=schema)
    ]
    return _offers(rows, token)


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


def _accepts_type(kind: ArgumentKind, type_: str) -> bool:
    if kind == "any":
        return True
    if kind == "integer":
        return type_.startswith(("int", "uint"))
    return type_ == kind


def _select_entries(session: Session, doc: str, token: str, schema: SchemaContext) -> list[Entry]:
    """`\\select` offers fields first, then the functions that apply to them.

    An open call is handled one level up, where its argument's span is known;
    here the token is the whole expression segment being typed.
    """
    columns = session.column_entries(doc, schema=schema)
    rows = [(0, name, detail, f"{name} ") for name, _, detail in columns]
    rows += [(1, fn, "aggregate", f"{fn}(") for fn in _aggregate_fns(schema)]
    rows += [(2, fn, "temporal part", f"{fn}(") for fn in _temporal_fns(columns, schema)]
    rows += [(3, fn, "window rank", f"{fn}() over (") for fn in _rank_fns(schema)]
    rows += [(4, fn, _scalar_detail(fn), f"{fn}(") for fn in _scalar_fns(schema)]
    return _offers(rows, token)


def _call_argument_entries(
    session: Session, doc: str, fn: str, partial: str, argument: int = 0,
    *, schema: SchemaContext,
) -> list[Entry]:
    """Complete the current argument without replacing its enclosing call."""
    columns = session.column_entries(doc, schema=schema)
    if fn in SCALAR_FUNCTIONS:
        if fn not in _scalar_fns(schema):
            return []
        spec = SCALAR_FUNCTIONS[fn]
        kind = spec.argument_kind(argument)
        if kind is None:
            return []
        # Multi-argument calls stay open: the user chooses additional optional
        # or variadic arguments. Unary calls complete like existing date parts.
        suffix = ")" if len(spec.parameters) == 1 else ", " if argument + 1 < spec.minimum else ""
        rows = [
            (0, name, detail, f"{name}{suffix}")
            for name, type_, detail in columns if _accepts_type(kind, type_)
        ]
        rows += [
            (1, nested, _scalar_detail(nested), f"{nested}(")
            for nested in _scalar_fns(schema)
            if kind == "any" or SCALAR_FUNCTIONS[nested].result == kind
        ]
        return _offers(rows, partial)
    if argument:
        return []
    if fn in _aggregate_fns(schema):
        rows = [(0, name, detail, f"{name})") for name, _, detail in columns]
        if fn == "count":
            rows.append((1, "*", "every row", "*)"))
        return _offers(rows, partial)
    families = {family for family, fns in schema.capabilities.functions.items() if fn in fns}
    if fn not in TEMPORAL or not families:
        return []
    rows = [
        (0, name, detail, f"{name})")
        for name, type_, detail in columns
        if dtype_family(type_) in families
    ]
    return _offers(rows, partial)


def _value_text(value: str) -> str:
    """A value as `\\where` wants it: bare when numeric, quoted otherwise."""
    return value if looks_numeric(value) else f'"{value}"'


def _value_entries(
    session: Session, doc: str, column: str, token: str, schema: SchemaContext,
) -> list[Entry]:
    """That column's distinct values, searched by what is being typed."""
    rows = [
        (0, value, f"{column} value", f"{_value_text(value)} ")
        for value in session.values_for(doc, column, schema=schema)
    ]
    return _offers(rows, token.strip('"'))[:VALUE_SUGGESTIONS]


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
    kept.sort(key=lambda row: row[0])
    return [Entry(label=call, insert=call, detail=detail, cursor_back=1) for _, _, call, detail in kept]


def _fn_view(schema: SchemaContext, token: str, slash: int) -> View | None:
    """The `\fn` library view: open an existing function, or create one.

    Bare `\fn` offers the library (and a new function). `\fn <name>` filters
    it to what matches and always offers to create the typed name — entering a
    new name is how a new function begins, and entering an existing one opens
    it. These are actions: accepting erases the whole span, so `\fn` never
    stays in the document.
    """
    entries: list[Entry] = []
    for fn in schema.fns:
        rank = match_rank(fn.name, token)
        if rank < 0:
            continue
        detail = f"{', '.join(fn.params)}" if fn.params else "no arguments"
        if fn.doc:
            detail = f"{detail} · {fn.doc}"
        entries.append(Entry(label=fn.name, detail=detail, action=f"fn-open:{fn.name}"))
    if not token or is_identifier(token):
        exact = any(entry.label.lower() == token.lower() for entry in entries)
        if not exact:
            label = token if token else "New function…"
            detail = f"create \\fn {token}" if token else "define a new table-valued function"
            entries.insert(0, Entry(label=label, detail=detail, action=f"fn-new:{token}"))
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


def view_for(
    session: Session, doc: str, line: str, column: int, *, parameters: Iterable[str] = (),
) -> View | None:
    """The palette view for the line text left of the caret, or `None`.

    `None` means "no palette": no `\\` before the caret, a command the palette
    has nothing to say about, or nothing left to suggest for what is typed.
    """
    schema = session.schema
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
    slash = before.rfind("\\")
    if slash < 0:
        return None
    word, gap, rest = _split(before[slash + 1 :])
    if word.lower() == FN_COMMAND:
        # `\fn` is a summon, not a clause: it never lands in the document. Bare
        # it opens the library; `\fn <name>` opens (or offers to create) that
        # one function. Accepting erases the whole `\ … ` span and fires the
        # app's action, exactly like `Run`/`Compile`.
        return _fn_view(schema, rest.strip(), slash)
    if not gap:
        entries = _command_entries(word, schema)
        return View(start=slash, token=word, entries=entries) if entries else None

    command = word.lower()
    arguments = DATASET_COMMANDS | SET_OP_COMMANDS | {"group", "where", "drop"}
    if command in arguments:
        token, offset = _word(rest)
        if command == "where":
            token, offset = _group_token(rest, token, offset)
    elif command == "select":
        position = _expression_position(rest)
        if position is None:
            return None
        offset, fn, argument = position
        token = rest[offset:].strip()
    else:
        segment, offset = _segment(rest)
        token = segment.strip()
    start = slash + 1 + len(word) + len(gap) + offset

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
            (0, name, detail, f"{name} ")
            for name, detail in session.dataset_entries(doc, schema=schema)
        ]
        if command in SET_OP_COMMANDS and not modifier:
            # The modifier leads the argument, and only once.
            rows += [(1, name, detail, f"{name} ") for name, detail in SET_OP_MODIFIERS]
        # Saved functions complete here too — accepted as a `name()` call with
        # the caret inside the parens — after the datasets, in match order.
        entries = _offers(rows, token) + _fn_call_entries(session, token, schema)
    elif command == "select":
        if fn is not None:
            entries = _call_argument_entries(session, doc, fn, token, argument, schema=schema)
        else:
            entries = _select_entries(session, doc, token, schema)
    elif command in COLUMN_COMMANDS:
        entries = _column_offers(session, doc, token, schema)
    elif command == "where":
        entries = _where_entries(session, doc, rest[:offset], token, schema)
    else:
        return None
    return View(start=start, token=token, entries=entries, phase="argument") if entries else None


def _where_entries(
    session: Session, doc: str, before: str, token: str, schema: SchemaContext,
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
    last_or = split_logic(before, "or")[-1]
    piece = split_logic(last_or, "and")[-1]
    # Inside a `( ... )` group the machine reads on inside the parens: peel
    # them and re-split, so `(a or b` offers columns where `b` is being typed
    # and a closed `(a = 1)` offers the joiners, exactly as the parser's
    # `_where_tree` descends.
    core = piece.strip()
    peeled = False
    while core.startswith("("):
        closed = take_paren(core)
        if closed is None:
            core = core[1:].strip()
        else:
            core = f"{closed[0]} {closed[1]}".strip()
        peeled = True
    if peeled:
        core = split_logic(core, "or")[-1]
        core = split_logic(core, "and")[-1]
        piece = core
    words = piece.split()
    if not words:
        # Either nothing is typed yet, or a joining `and`/`or` just landed
        # (its keyword is the only reason the last piece is blank): the next
        # thing a condition wants is a column.
        return _column_offers(session, doc, token, schema)
    head = where_head(piece.strip())
    if head is None:
        if len(words) == 1 and _COL_PATTER.match(words[0]):
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
    col = column.split(".")[-1]
    if op in NULL_OPS:
        # `is [not] null` needs no operand: the condition is complete.
        return _joiners(token)
    if op == "between":
        return _between_entries(session, doc, col, tail, token, schema)
    if op in REGEX_OPS:
        # A regex pattern is typed, never offered from the value pool.
        return _joiners(token) if tail and _closed_operand(tail) else []
    if op in SUBQUERY_OPS:
        # An `in` takes a `( … )` — a list or a subquery — which is typed.
        return _joiners(token) if tail.endswith(")") else []
    if not tail:
        return _value_entries(session, doc, col, token, schema)
    if _closed_operand(tail):
        return _joiners(token)
    if tail.startswith('"'):
        return _value_entries(session, doc, col, token, schema)
    return []


def _joiners(token: str) -> list[Entry]:
    """`and`/`or`: the rows that follow any complete condition."""
    return _offers(
        [(0, joiner, "join conditions", f"{joiner} ") for joiner in _JOINERS],
        token,
    )


def _between_entries(
    session: Session, doc: str, col: str, tail: str, token: str, schema: SchemaContext,
) -> list[Entry]:
    """`between`'s low, its `and`, then its high — each an offer in turn."""
    bounds = [bound.strip() for bound in split_logic(tail, "and")]
    if len(bounds) == 1:
        if not bounds[0] or not _closed_operand(bounds[0]):
            return _value_entries(session, doc, col, token, schema)
        return _offers([(0, "and", "between's high bound", "and ")], token)
    high = bounds[1]
    if not high or not _closed_operand(high):
        return _value_entries(session, doc, col, token, schema)
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
        self._labels: list[str] = []
        # True after Escape: the offers stay shut until a `\` (or Escape) lifts it.
        self._dismissed = False

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
        row, column = editor.cursor_location
        line = editor.document[row]
        if column > 0 and (
            line[column - 1] == "\\"
            or (self.parameters is not None and line[column - 1] == "@" and _parameter_token(line[:column]))
        ):
            # An explicit `\` or function parameter `@` lifts Escape's dismissal.
            self._dismissed = False
        with self.session.target_source(self.source_id):
            view = (
                view_for(
                    self.session, editor.text, line, column,
                    parameters=self.parameters() if self.parameters is not None else (),
                )
                if self.session.intellisense and not self._dismissed
                else None
            )
        if view is not None and not self.workspace_actions:
            view = View(view.start, view.token, [e for e in view.entries if not e.action], view.phase, view.end)
            if not view.entries:
                view = None
        if view is None:
            self.close()
            return
        if view.labels != self._labels:
            self.clear_options()
            self.add_options(
                [
                    Option(prompt(entry.label, entry.detail), id=f"opt-{index}")
                    for index, entry in enumerate(view.entries)
                ]
            )
            self._labels = view.labels
            self.highlighted = 0
        elif self.highlighted is None:
            self.highlighted = 0
        self._view = view
        self._place()
        if not self.is_open:
            self.add_class("open")
            self.scroll_to_highlight()

    def _place(self) -> None:
        """Float the popup just under the caret, no taller than the pane allows."""
        pane = self.parent
        editor = self.editor
        if editor is None or pane is None:
            return
        caret_row = editor.cursor_location[0] - int(editor.scroll_offset.y)
        top = min(max(caret_row + 2, 1), max(pane.content_size.height - 3, 1))
        self.styles.offset = (1, top)
        self.styles.max_height = max(pane.content_size.height - top - 1, 4)

    def close(self) -> None:
        """Hide the palette without touching the document."""
        self._view = None
        self._labels = []
        self.remove_class("open")

    def escape(self) -> None:
        """Escape shows and hides the popup — the same key, both ways.

        Open (or offering something), it closes and stays shut while the user
        keeps typing; closed, it lifts that dismissal and offers again.
        """
        if self.is_open:
            self.close()
            self._dismissed = True
            return
        self._dismissed = False
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

    def accept_highlighted(self) -> bool:
        """Accept the highlighted row; False when there is nothing to accept."""
        index = self.highlighted
        return False if index is None else self.accept(index)

    def accept(self, index: int) -> bool:
        """Apply row `index`; False when accepting it would change nothing.

        The palette is not closed on the way out: the accept ends by syncing to
        the caret it just moved, so the offers at the landing spot are already
        there — `\\sel` completes to `\\select ` with the fields the new clause
        takes, and `sum(` keeps offering its argument.
        """
        view = self._view
        editor = self.editor
        if view is None or editor is None or not 0 <= index < len(view.entries):
            return False
        entry = view.entries[index]
        if not entry.action and view.highlighted_is_typed(index):
            return False
        row, column = editor.cursor_location
        if entry.action:
            editor.replace("", (row, view.start), (row, column))
            editor.cursor_location = (row, view.start)
            self.post_message(self.ActionPerformed(entry.action))
        else:
            command = entry.insert.strip()
            if command.startswith("\\") and command[1:].lower() in CLAUSE_NAMES:
                self._take_clause(editor, row, column, view, command[1:].lower())
            else:
                end = column if view.end is None else view.end
                editor.replace(entry.insert, (row, view.start), (row, end))
                editor.cursor_location = (row, view.start + len(entry.insert) - entry.cursor_back)
        self.sync()
        return True

    def _take_clause(self, editor, row: int, column: int, view: View, name: str) -> None:
        """A clause command takes its own line — or the line its clause has.

        The lookup runs in the block the caret is editing (`clause_line`), so a
        `\\where` under a `\\with` and the document's own `\\where` are different
        clauses. Finding one takes the caret to it and writes nothing; otherwise
        the command breaks the line first, so it never lands after whatever the
        caret happened to be sitting in.
        """
        target = clause_line(editor.text, row + 1, name)
        if target is not None and target != row + 1:
            editor.replace("", (row, view.start), (row, column))
            editor.cursor_location = (target - 1, len(editor.document[target - 1]))
            return
        before = editor.document[row][: view.start]
        after = editor.document[row][column:]
        indent = before[: len(before) - len(before.lstrip())]
        prefix = f"\n{indent}" if before.strip() else ""
        suffix = f"\n{indent}" if after.strip() else ""
        command = f"\\{name} "
        editor.replace(f"{prefix}{command}{suffix}", (row, view.start), (row, column))
        line, start = (row + 1, len(indent)) if prefix else (row, view.start)
        editor.cursor_location = (line, start + len(command))


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
        if palette.is_open and palette.accept_highlighted():
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

    def action_palette_tab(self) -> None:
        """`tab` accepts the highlighted suggestion — it never moves focus."""
        if self.palette.is_open:
            self.palette.accept_highlighted()
