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
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

from textual.content import Content
from textual.message import Message
from textual.widgets import OptionList
from textual.widgets.option_list import Option

from anyql.query import AGGREGATES, TEMPORAL, capabilities, clause_line, dtype_family

from .session import Session, looks_numeric

# The clause commands, in document order, with the detail line the palette shows.
COMMANDS: tuple[tuple[str, str], ...] = (
    ("from", "set the source dataset"),
    ("open", "same as \\from — the dataset spelling"),
    ("join", "append an inner join"),
    ("select", "append a projection"),
    ("where", "set the row filter"),
    ("group", "append a grouping key"),
    ("order", "append an ordering"),
    ("case", "add a computed column"),
    ("limit", "cap the rows"),
    ("with", "start a CTE block"),
)

# Palette actions: label, detail, and the action name the app carries out.
# Actions are the app's own — they never enter the document or the AST.
ACTIONS: tuple[tuple[str, str, str], ...] = (
    ("Run", "ctrl+enter · execute the document", "run"),
    ("Compile", "ctrl+k · render SQL without running", "compile"),
    ("Data source…", "ctrl+o · add a D1 database", "add-source"),
    ("History", "show/hide the history pane", "history"),
    ("Results", "show/hide the results pane", "toggle-results"),
    ("SQL", "show/hide the SQL pane", "toggle-sql"),
    ("Schema", "show/hide the schema pane", "toggle-schema"),
    ("Settings", "every setting, full screen", "settings"),
)

# Commands whose argument is a dataset name, and whose argument is a column name.
DATASET_COMMANDS = frozenset({"from", "open", "join"})
COLUMN_COMMANDS = frozenset({"select", "group", "order"})

# The ten clause commands: accepting one takes its own line, or the line the
# current block already gives that clause.
CLAUSE_NAMES = frozenset(name for name, _ in COMMANDS)

# Fallback comparison operators; the live capability set is preferred.
DEFAULT_OPERATORS: tuple[str, ...] = ("=", "!=", ">", ">=", "<", "<=", "like")

# How many value rows the popup shows; the search still reads the whole pool.
VALUE_SUGGESTIONS = 50

# `sum(amount` — a call whose argument is still being typed (no `)` yet).
_CALL_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\(([^()]*)$")


def operators() -> tuple[str, ...]:
    """The operators the active backend advertises for `\\where` and `\\case`."""
    live = tuple(capabilities().operators)
    return live or DEFAULT_OPERATORS


@dataclass(frozen=True)
class Entry:
    """One palette row: what it reads as, and what accepting it does."""

    label: str
    insert: str = ""
    detail: str = ""
    action: str = ""


@dataclass(frozen=True)
class View:
    """The palette's current content and the document span an accept replaces."""

    start: int
    token: str
    entries: list[Entry] = field(default_factory=list)
    # "command": the word right after the `\\`. "argument": everything a command
    # takes. Escape's dismissal silences both; a fresh `\\` is what lifts it.
    phase: Literal["command", "argument"] = "command"

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


def _command_entries(token: str) -> list[Entry]:
    """The language and the app's actions matching the word after the `\\`."""
    commands = [
        (match_rank(name, token), index, Entry(label=f"\\{name}", insert=f"\\{name} ", detail=detail))
        for index, (name, detail) in enumerate(COMMANDS)
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


def _word(rest: str) -> tuple[str, int]:
    """The word the caret is in (or right after) and its offset in `rest`.

    `\\group` and `\\where` take whitespace-separated arguments, so what is
    being completed there is the last word — not the whole comma segment.
    """
    if not rest or rest[-1].isspace():
        return "", len(rest)
    token = rest.split()[-1]
    return token, rest.rfind(token)


def _column_offers(session: Session, doc: str, token: str) -> list[Entry]:
    """The document's open-table columns as palette rows."""
    rows = [(0, name, detail, f"{name} ") for name, _, detail in session.column_entries(doc)]
    return _offers(rows, token)


def _aggregate_fns() -> list[str]:
    """The aggregates the active backend advertises that the language knows."""
    advertised = set(capabilities().aggregates)
    return [fn for fn in AGGREGATES if fn in advertised]


def _rank_fns() -> list[str]:
    """Rank functions — `<fn>() over ( ... )` with no argument of their own."""
    advertised = set(capabilities().window_functions)
    return [fn for fn in ("rank", "dense_rank", "row_number") if fn in advertised]


def _temporal_fns(columns: list[tuple[str, str, str]]) -> list[str]:
    """Temporal extractions at least one of the open columns can carry."""
    families = {dtype_family(type_) for _, type_, _ in columns}
    return [
        fn
        for fn in TEMPORAL
        if any(fn in capabilities().functions.get(family, []) for family in families if family)
    ]


def _select_entries(session: Session, doc: str, token: str) -> list[Entry]:
    """`\\select` offers fields first, then the functions that apply to them.

    An open call is handled one level up, where its argument's span is known;
    here the token is the whole expression segment being typed.
    """
    columns = session.column_entries(doc)
    rows = [(0, name, detail, f"{name} ") for name, _, detail in columns]
    rows += [(1, fn, "aggregate", f"{fn}(") for fn in _aggregate_fns()]
    rows += [(2, fn, "temporal part", f"{fn}(") for fn in _temporal_fns(columns)]
    rows += [(3, fn, "window rank", f"{fn}() over (") for fn in _rank_fns()]
    return _offers(rows, token)


def _call_argument_entries(session: Session, doc: str, fn: str, partial: str) -> list[Entry]:
    """The argument of an open `fn(` — only the columns that function takes."""
    columns = session.column_entries(doc)
    if fn in _aggregate_fns():
        rows = [(0, name, detail, f"{name})") for name, _, detail in columns]
        if fn == "count":
            rows.append((1, "*", "every row", "*)"))
        return _offers(rows, partial)
    families = {family for family, fns in capabilities().functions.items() if fn in fns}
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


def _value_entries(session: Session, doc: str, column: str, token: str) -> list[Entry]:
    """That column's distinct values, searched by what is being typed."""
    rows = [
        (0, value, f"{column} value", f"{_value_text(value)} ")
        for value in session.values_for(doc, column)
    ]
    return _offers(rows, token.strip('"'))[:VALUE_SUGGESTIONS]


def view_for(session: Session, doc: str, line: str, column: int) -> View | None:
    """The palette view for the line text left of the caret, or `None`.

    `None` means "no palette": no `\\` before the caret, a command the palette
    has nothing to say about, or nothing left to suggest for what is typed.
    """
    before = line[:column]
    slash = before.rfind("\\")
    if slash < 0:
        return None
    word, gap, rest = _split(before[slash + 1 :])
    if not gap:
        entries = _command_entries(word)
        return View(start=slash, token=word, entries=entries) if entries else None

    command = word.lower()
    arguments = DATASET_COMMANDS | {"group", "where"}
    if command in arguments:
        token, offset = _word(rest)
    else:
        segment, offset = _segment(rest)
        token = segment.strip()
    start = slash + 1 + len(word) + len(gap) + offset

    if command in DATASET_COMMANDS:
        if rest[:offset].split():
            # `\from events |` is done: a table is already named there, and the
            # rest of the clause (`as alias`, `on col`) takes no dataset — so a
            # stray Enter cannot append a second one.
            return None
        rows = [(0, name, detail, f"{name} ") for name, detail in session.dataset_entries()]
        entries = _offers(rows, token)
    elif command == "select":
        call = _CALL_RE.match(token)
        if call is not None:
            # An open call completes its own argument: the typed span starts
            # after `fn(`, so accepting leaves the call in place.
            entries = _call_argument_entries(session, doc, call.group(1).lower(), call.group(2))
            start += len(call.group(1)) + 1
            token = call.group(2)
        else:
            entries = _select_entries(session, doc, token)
    elif command in COLUMN_COMMANDS:
        entries = _column_offers(session, doc, token)
    elif command == "where":
        entries = _where_entries(session, doc, rest[:offset].split(), token)
    else:
        return None
    return View(start=start, token=token, entries=entries, phase="argument") if entries else None


def _where_entries(session: Session, doc: str, head: list[str], token: str) -> list[Entry]:
    """`\\where` offers a column, then an operator, then that column's values."""
    if len(head) >= 2 and head[1].lower() in operators():
        value_so_far = " ".join(head[2:])
        if value_so_far and value_so_far.count('"') % 2 == 0:
            # `\where event_type = "purchase" |` is done: a `\where` carries one
            # value (a later one replaces it), so nothing follows a closed one —
            # and a stray Enter cannot append a second value to the clause.
            return []
        return _value_entries(session, doc, head[0].split(".")[-1], token)
    if len(head) == 1:
        rows = [(0, op, "operator", f"{op} ") for op in operators()]
        return _offers(rows, token)
    if head:
        return []
    return _column_offers(session, doc, token)


class CommandPalette(OptionList):
    """The overlay itself: an option list that is driven, never focused."""

    can_focus = False

    class ActionPerformed(Message):
        """A palette action (`Run`, `Compile`, …) for the app to carry out."""

        def __init__(self, action: str) -> None:
            self.action = action
            super().__init__()

    def __init__(self, session: Session, **kwargs) -> None:
        super().__init__(**kwargs)
        self.session = session
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

    def sync(self) -> None:
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
        if editor is None:
            return
        row, column = editor.cursor_location
        line = editor.document[row]
        if column > 0 and line[column - 1] == "\\":
            # An explicit `\` is a summon: it lifts Escape's dismissal.
            self._dismissed = False
        view = (
            view_for(self.session, editor.text, line, column)
            if self.session.intellisense and not self._dismissed
            else None
        )
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
                editor.replace(entry.insert, (row, view.start), (row, column))
                editor.cursor_location = (row, view.start + len(entry.insert))
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
