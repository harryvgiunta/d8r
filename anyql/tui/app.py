"""anyQL's Textual application: editor, explorer, results — all in-process.

There is no webserver behind this app and none in front of it: the document is
parsed by `anyql.query`, executed by `anyql.engine`, and everything the widgets
show comes back as plain Python. The TUI is a *consumer* of those two layers —
it never re-implements a rule of the language or the engine.
"""

from __future__ import annotations

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.content import Content, Text
from textual.geometry import Size
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    DataTable,
    OptionList,
    Select,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
    Tree,
)

from anyql.engine import DIALECTS, DIALECT_BY_NAME, capabilities_for
from anyql.query import ColumnDef, table_by_name

from .add_source import AddSourceModal
from .palette import CommandPalette
from .session import RunOutcome, Session
from .settings import SettingsScreen

# The document a fresh session opens with: a real query against the demo
# source, so `ctrl+enter` does something true on the very first keystroke.
WELCOME_DOCUMENT = """\\from events
\\select event_type
\\select sum(amount) as total
\\group event_type
\\order total desc
\\limit 10
"""

KEY_HINTS = "ctrl+enter run · ctrl+k compile · ctrl+o data source · ctrl+comma settings · \\ palette · ctrl+q quit"

MIN_COLUMN_WIDTH = 4


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
        return self.query_one("#editor", TextArea)

    @property
    def palette(self) -> CommandPalette:
        """The `\\` overlay."""
        return self.query_one("#palette", CommandPalette)

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


class IdeScreen(Screen):
    """The IDE's own screen: no key walks the panes.

    Textual's `Screen` binds `tab`/`shift+tab` to focus next/previous, and its
    `TextArea` moves focus on `tab` from its own key handler
    (`tab_behavior="focus"`) — and inherited bindings survive a subclass's
    `BINDINGS`, so the list is written out instead: both keys are claimed as
    no-ops, and a claimed key is consumed before the focused widget sees it. In
    the document `EditorPane`'s own `tab` wins the chain, and there it accepts
    the intellisense suggestion. Modals keep Textual's behaviour — walking an
    add-source form's fields with tab is what those fields are for.
    """

    BINDINGS = [
        Binding("tab", "noop", "Tab", show=False),
        Binding("shift+tab", "noop", "Shift Tab", show=False),
        Binding("ctrl+c,super+c", "screen.copy_text", "Copy selected text", show=False),
    ]

    def action_noop(self) -> None:
        """Claim a key and do nothing with it (the pane above may have it)."""


class ResultsTable(DataTable):
    """The data explorer: DataTable plus keyboard column resizing.

    Everything else here is Textual's: the cell cursor doubles as the column
    cursor, arrows and page keys scroll both axes, and the view follows the
    cursor. Resizing is the one thing DataTable leaves to its owner.
    """

    BINDINGS = [
        Binding("shift+left", "narrow_column", "Narrow column", show=False),
        Binding("shift+right", "widen_column", "Widen column", show=False),
    ]

    def action_narrow_column(self) -> None:
        self._resize_column(-2)

    def action_widen_column(self) -> None:
        self._resize_column(2)

    def _resize_column(self, delta: int) -> None:
        columns = self.ordered_columns
        if not columns:
            return
        index = min(self.cursor_column, len(columns) - 1)
        column = columns[index]
        before = column.get_render_width(self)
        if column.auto_width:
            # Pin the width it renders at today, then resize from there.
            column.width = before - 2 * self.cell_padding
            column.auto_width = False
        column.width = max(MIN_COLUMN_WIDTH, column.width + delta)
        after = column.get_render_width(self)
        width, height = self.virtual_size
        self.virtual_size = Size(width + after - before, height)
        self._clear_caches()
        self.refresh_column(index)


def column_label(column: ColumnDef) -> Text:
    """A column row in the schema explorer: the name, then its dtype."""
    return Text.assemble(column.name, "  ", (column.type, "dim"))


def dataset_label(name: str, rows: int) -> Text:
    """A dataset row in the schema explorer: the name, then its row count."""
    return Text.assemble((name, "bold"), "  ", (f"{rows} rows", "dim"))


class AnyqlApp(App):
    """The data IDE: a `\\command` document, the live schema, and the rows."""

    CSS_PATH = "app.tcss"
    TITLE = "anyQL"
    SUB_TITLE = "data IDE"

    # The panes `\results`, `\sql`, `\history` and `\schema` show and hide, and
    # the names the Settings menu lists them under. Results, SQL and History are
    # tabs of the result view; the schema explorer is its own container.
    TAB_PANES: dict[str, str] = {
        "results": "tab-results",
        "sql": "tab-sql",
        "history": "tab-history",
    }
    PANES: dict[str, str] = {**TAB_PANES, "schema": "#schema-pane"}
    PANE_TITLES: dict[str, str] = {
        "results": "Results pane",
        "sql": "SQL pane",
        "history": "History pane",
        "schema": "Schema pane",
    }

    # Priority bindings so they win over the focused editor's own keys — the
    # editor claims ctrl+k for "delete to line end" and the app needs it.
    BINDINGS = [
        Binding("ctrl+enter", "run", "Run", priority=True),
        Binding("f5", "run", "Run", priority=True, show=False),
        Binding("ctrl+k", "compile", "Compile", priority=True),
        Binding("f6", "compile", "Compile", priority=True, show=False),
        Binding("ctrl+o", "add_source", "Data source", priority=True),
        # The way back into Settings when the Intellisense switch has the
        # palette shut — `\settings` itself needs the popup.
        Binding("ctrl+comma", "settings", "Settings", priority=True),
    ]

    def __init__(self, session: Session | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.theme = "textual-dark"
        self.session = session if session is not None else Session()
        self.history_documents: list[str] = []

    def get_default_screen(self) -> Screen:
        """The IDE runs on `IdeScreen`, whose tab does not walk the panes."""
        return IdeScreen(id="_default")

    # -- layout -------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="header"):
            yield Static("anyQL", id="app-title")
            yield Select(
                self._source_options(),
                allow_blank=False,
                value=self.session.active_id,
                id="source-select",
            )
            yield Select(
                self._dialect_options(),
                allow_blank=False,
                value=self.session.dialect,
                id="dialect-select",
            )
            yield Static("", id="backend-pill")
        with Horizontal(id="body"):
            with Vertical(id="schema-pane"):
                yield Static("Schema", classes="pane-title")
                yield Tree(Text("datasets"), id="schema-tree")
            with Vertical(id="work-bench"):
                with EditorPane(id="editor-pane"):
                    yield Static("Document", classes="pane-title")
                    yield TextArea(WELCOME_DOCUMENT, show_line_numbers=True, id="editor")
                    yield CommandPalette(self.session, id="palette", markup=False)
                with TabbedContent(id="result-tabs"):
                    with TabPane("Results", id="tab-results"):
                        yield Static("", id="results-error", markup=False)
                        yield ResultsTable(id="results-table", zebra_stripes=True, cursor_type="cell")
                        yield Static("", id="results-status", markup=False)
                    with TabPane("SQL", id="tab-sql"):
                        yield TextArea("", read_only=True, soft_wrap=False, id="sql-text")
                    with TabPane("History", id="tab-history"):
                        yield DataTable(id="history-table", zebra_stripes=True, cursor_type="row")
        with Vertical(id="footer"):
            yield Static(KEY_HINTS, id="keymap")
            yield Static("", id="status", markup=False)

    def on_mount(self) -> None:
        self.palette.attach(self.editor)
        self._refresh_header()
        self._refresh_tree()
        self.query_one("#history-table", DataTable).add_columns(
            "time", "source", "dialect", "rows", "ms", "document"
        )
        self._set_status(f"ready · {self.session.active_id} · {self.session.dialect}")
        self.editor.focus()

    # -- widgets ------------------------------------------------------------

    @property
    def editor(self) -> TextArea:
        """The document editor."""
        return self.query_one("#editor", TextArea)

    @property
    def palette(self) -> CommandPalette:
        """The `\\` overlay."""
        return self.query_one("#palette", CommandPalette)

    def _source_options(self) -> list[tuple[Content, str]]:
        return [(Content(source.display), source.id) for source in self.session.sources.values()]

    def _dialect_options(self) -> list[tuple[Content, str]]:
        """Every advertised compile target; unavailable ones say so."""
        options: list[tuple[Content, str]] = []
        for spec in DIALECTS:
            label = spec["label"] if spec["compiles"] else f"{spec['label']} · unavailable"
            options.append((Content(label), spec["name"]))
        return options

    def _refresh_header(self) -> None:
        backend = capabilities_for(self.session.source)["backend"]
        self.query_one("#backend-pill", Static).update(backend)

    def _sync_dialect_select(self) -> None:
        select = self.query_one("#dialect-select", Select)
        if select.value != self.session.dialect:
            select.value = self.session.dialect

    def _sync_source_select(self) -> None:
        select = self.query_one("#source-select", Select)
        if select.value != self.session.active_id:
            select.value = self.session.active_id

    def _refresh_tree(self) -> None:
        """Rebuild the explorer from the active source (columns load on expand)."""
        tree = self.query_one("#schema-tree", Tree)
        tree.clear()
        root = tree.root
        root.expand()
        datasets = self.session.source.datasets
        for name, entry in datasets.items():
            node = root.add(
                dataset_label(name, int(entry["rows"])),
                data={"kind": "dataset", "name": name},
                allow_expand=True,
            )
            # A placeholder keeps the node expandable; expanding replaces it
            # with the columns the schema registry holds for that dataset.
            node.add_leaf(Text("…", style="dim"), data={"kind": "placeholder"})

    def _refresh_history(self) -> None:
        table = self.query_one("#history-table", DataTable)
        table.clear()
        self.history_documents = []
        for entry in self.session.history:
            table.add_row(
                entry.at,
                entry.source,
                entry.dialect,
                str(entry.rows),
                f"{entry.ms:.1f}",
                Text(entry.title),
            )
            self.history_documents.append(entry.doc)

    # -- status -------------------------------------------------------------

    def _set_status(self, message: str) -> None:
        self.query_one("#status", Static).update(message)

    def _set_sql(self, sql: str) -> None:
        self.query_one("#sql-text", TextArea).load_text(sql)

    def _set_error(self, message: str) -> None:
        """The Results tab's error line; empty text hides it again."""
        widget = self.query_one("#results-error", Static)
        widget.update(message)
        widget.set_class(bool(message), "error")

    def _show_tab(self, tab_id: str) -> None:
        """Reveal a result tab — a pane the user hid is never forced back."""
        for name, pane_id in self.TAB_PANES.items():
            if pane_id == tab_id and not self.pane_visible(name):
                return
        self.query_one("#result-tabs", TabbedContent).active = tab_id

    # -- panes --------------------------------------------------------------

    def pane_visible(self, name: str) -> bool:
        """True while that pane (`results`, `sql`, `history`, `schema`) is shown."""
        if name == "schema":
            return self.query_one(self.PANES[name]).display
        return self.query_one("#result-tabs", TabbedContent).get_tab(self.PANES[name]).display

    def set_pane(self, name: str, visible: bool) -> None:
        """Show or hide one pane; hiding the active tab moves to a visible one."""
        if name == "schema":
            self.query_one(self.PANES[name]).display = visible
            return
        tabs = self.query_one("#result-tabs", TabbedContent)
        pane_id = self.PANES[name]
        if visible:
            tabs.show_tab(pane_id)
            # Showing a pane means wanting to see it: it comes to the front.
            tabs.active = pane_id
        else:
            tabs.hide_tab(pane_id)
        self._fit_result_tabs()

    def _fit_result_tabs(self) -> None:
        """Hand the document the whole bench once no result tab is left.

        Hiding Results, SQL and History is a legitimate choice, and then the tab
        strip and an empty pane would hold half the screen for nothing.
        """
        shown = any(self.pane_visible(name) for name in self.TAB_PANES)
        self.query_one("#work-bench").set_class(not shown, "no-tabs")

    def toggle_pane(self, name: str) -> bool:
        """Flip one pane; returns it as it now stands."""
        self.set_pane(name, not self.pane_visible(name))
        return self.pane_visible(name)

    def _modal_open(self) -> bool:
        """True while a modal screen owns the keyboard."""
        return isinstance(self.screen, ModalScreen)

    # -- actions ------------------------------------------------------------

    def action_run(self) -> None:
        """Parse the document and execute it; a user mistake never crashes."""
        if self._modal_open():
            return
        self.palette.close()
        outcome = self.session.run(self.editor.text)
        self._set_error(outcome.error)
        self._render_results(outcome)
        self._refresh_history()
        self._set_status(outcome.status)
        self._show_tab("tab-results")

    def action_compile(self) -> None:
        """Render the document as SQL for the active dialect — never executes."""
        if self._modal_open():
            return
        self.palette.close()
        sql, message = self.session.compile(self.editor.text)
        if sql is None:
            self._set_status(message)
            return
        self._set_sql(sql)
        self._set_status(message)
        self._show_tab("tab-sql")

    def action_add_source(self) -> None:
        """Open the add-source modal; the built source registers on `Add`."""
        if self._modal_open():
            return
        self.palette.close()
        self.push_screen(AddSourceModal(self.session), self._source_added)

    def action_toggle_results(self) -> None:
        """`\\results` — show or hide the results pane."""
        self._toggle_pane("results")

    def action_toggle_sql(self) -> None:
        """`\\sql` — show or hide the SQL pane."""
        self._toggle_pane("sql")

    def action_toggle_history(self) -> None:
        """`\\history` — show or hide the history pane."""
        self._toggle_pane("history")

    def action_toggle_schema(self) -> None:
        """`\\schema` — show or hide the schema pane."""
        self._toggle_pane("schema")

    def _toggle_pane(self, name: str) -> None:
        if self._modal_open():
            return
        self.palette.close()
        visible = self.toggle_pane(name)
        self._set_status(f"{self.PANE_TITLES[name].lower()} {'shown' if visible else 'hidden'}")

    def action_settings(self) -> None:
        """`\\settings` — open the full-screen settings menu."""
        if self._modal_open():
            return
        self.palette.close()
        self.push_screen(SettingsScreen(self))

    def _render_results(self, outcome: RunOutcome) -> None:
        table = self.query_one("#results-table", ResultsTable)
        table.clear(columns=True)
        if not outcome.error and outcome.columns:
            table.add_columns(
                *[
                    Text.assemble(name, (" · " + dtype, "dim")) if dtype else Text(name)
                    for name, dtype in zip(outcome.columns, outcome.dtypes)
                ]
            )
            if outcome.rows:
                table.add_rows([[self._cell(value) for value in row] for row in outcome.rows])
                table.move_cursor(row=0, column=0)
        self.query_one("#results-status", Static).update(outcome.status)

    @staticmethod
    def _cell(value) -> Text:
        """One result cell; a null renders as NULL rather than as empty text."""
        if value is None:
            return Text("NULL", style="dim")
        return Text(str(value))

    def _source_added(self, source) -> None:
        """Register a source the modal built, then re-point the whole app at it."""
        if source is None:
            return
        self.session.register(source)
        self.query_one("#source-select", Select).set_options(self._source_options())
        self._sync_source_select()
        self._sync_dialect_select()
        self._refresh_header()
        self._refresh_tree()
        self._set_status(f"{source.id} added · {len(source.datasets)} tables")

    # -- messages -----------------------------------------------------------

    @on(TextArea.Changed, "#editor")
    def _editor_changed(self) -> None:
        """The document changed: the palette follows it, and offers on it."""
        self.palette.sync()

    @on(TextArea.SelectionChanged, "#editor")
    def _editor_caret_moved(self) -> None:
        """The caret moved on its own: an open palette follows it, and no more.

        Summoning on every arrow key would put the popup in front of the
        document's own navigation — moving the caret is not a request for
        offers, and the palette's keys would swallow the next move.
        """
        if self.palette.is_open:
            self.palette.sync()

    @on(Select.Changed, "#source-select")
    def _source_changed(self, event: Select.Changed) -> None:
        if isinstance(event.value, str):
            self.select_source(event.value)

    def select_source(self, source_id: str) -> None:
        """Point the session — and everything rendered from it — at a source.

        One path for the header's select and the Settings menu's Data source
        rows, so both leave the header, the tree, the dialect and the pill in
        the same state.
        """
        if source_id not in self.session.sources or source_id == self.session.active_id:
            return
        self.session.set_active(source_id)
        self._sync_source_select()
        self._refresh_header()
        self._refresh_tree()
        self._sync_dialect_select()
        self._set_status(f"{self.session.active_id} active · schema reloaded for the parser and the palette")

    @on(Select.Changed, "#dialect-select")
    def _dialect_changed(self, event: Select.Changed) -> None:
        if isinstance(event.value, str):
            self.select_dialect(event.value)

    def select_dialect(self, name: str) -> None:
        """Choose the one compile dialect every source renders for.

        The header's select and the Settings menu's Dialect rows both land here;
        compiling never executes anything.
        """
        if name == self.session.dialect:
            return
        self.session.dialect = name
        self._sync_dialect_select()
        spec = DIALECT_BY_NAME.get(name)
        if spec is not None and not spec["compiles"]:
            self._set_status(f"{name} is known but does not compile in this build")
            return
        sql, message = self.session.compile(self.editor.text)
        if sql is None:
            self._set_status(f"dialect {name} · not compiled: {message}")
            return
        self._set_sql(sql)
        self._set_status(f"dialect {name}")

    @on(Tree.NodeExpanded, "#schema-tree")
    def _dataset_expanded(self, event: Tree.NodeExpanded) -> None:
        """Load a dataset's columns from the schema registry, once."""
        node = event.node
        data = node.data if isinstance(node.data, dict) else {}
        if data.get("kind") != "dataset" or data.get("loaded"):
            return
        node.remove_children()
        table = table_by_name(data["name"])
        if table is None:
            node.add_leaf(Text("not in the registry", style="dim"), data={"kind": "note"})
        else:
            for column in table.columns:
                node.add_leaf(column_label(column), data={"kind": "column", "name": column.name})
        data["loaded"] = True

    @on(Tree.NodeSelected, "#schema-tree")
    def _column_selected(self, event: Tree.NodeSelected) -> None:
        """Selecting a column puts its name into the document."""
        data = event.node.data if isinstance(event.node.data, dict) else {}
        if data.get("kind") != "column":
            return
        self.editor.insert(data["name"])
        self.editor.focus()
        self.palette.sync()
        self._set_status(f"inserted {data['name']}")

    @on(DataTable.RowSelected, "#history-table")
    def _history_selected(self, event: DataTable.RowSelected) -> None:
        """Selecting a history row loads that document back into the editor."""
        if 0 <= event.cursor_row < len(self.history_documents):
            self._load_document(self.history_documents[event.cursor_row])

    def _load_document(self, document: str) -> None:
        self.editor.load_text(document)
        self.palette.close()
        self.editor.focus()
        self._set_status("document loaded from history")

    @on(OptionList.OptionSelected, "#palette")
    def _palette_clicked(self, event: OptionList.OptionSelected) -> None:
        """A click accepts that row, exactly like Enter on the highlight."""
        self.palette.accept(event.option_index)
        self.editor.focus()

    @on(CommandPalette.ActionPerformed)
    def _palette_action(self, event: CommandPalette.ActionPerformed) -> None:
        actions = {
            "run": self.action_run,
            "compile": self.action_compile,
            "add-source": self.action_add_source,
            "history": self.action_toggle_history,
            "toggle-results": self.action_toggle_results,
            "toggle-sql": self.action_toggle_sql,
            "toggle-schema": self.action_toggle_schema,
            "settings": self.action_settings,
        }
        action = actions.get(event.action)
        if action is not None:
            action()


def main() -> None:
    """`python -m anyql` — run the app."""
    AnyqlApp().run()


__all__ = ["AnyqlApp", "CommandPalette", "EditorPane", "ResultsTable", "main"]
