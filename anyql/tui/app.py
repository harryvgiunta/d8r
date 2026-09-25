"""anyQL's Textual application: editor, explorer, results — all in-process.

There is no webserver behind this app and none in front of it: the document is
parsed by `anyql.query`, executed by `anyql.engine`, and everything the widgets
show comes back as plain Python. The TUI is a *consumer* of those two layers —
it never re-implements a rule of the language or the engine.
"""

from __future__ import annotations

import asyncio

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.content import Content, Text
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    DataTable,
    Select,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
    Tree,
)

from anyql.ai.context import AIProposal
from anyql.engine import DIALECTS, DIALECT_BY_NAME, capabilities_for
from anyql.query import ColumnDef

from .add_source import AddSourceModal
from .ai import AIPanel, AITarget
from .palette import CommandPalette, EditorPane
from .results import ResultsTable
from .session import RunOutcome, Session
from .settings import SettingsScreen
from .fn import FnScreen

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
        Binding("ctrl+enter", "run_or_chat", "Run", priority=True),
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
        self._document_identity = 0
        self.run_busy = False
        self._run_task: asyncio.Task[RunOutcome] | None = None
        self._closing = False

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
                    with Horizontal(id="document-actions"):
                        yield Static("Document", classes="pane-title")
                        yield Button("To function", id="query-to-fn", compact=True)
                    yield TextArea(WELCOME_DOCUMENT, show_line_numbers=True, id="editor")
                    yield CommandPalette(self.session, id="palette", markup=False)
                with TabbedContent(id="result-tabs"):
                    with TabPane("Results", id="tab-results"):
                        yield Static("", id="results-error", markup=False)
                        with Horizontal(id="results-actions"):
                            yield Button("Copy rows", id="results-copy", compact=True)
                            yield Button("Export CSV…", id="results-export", compact=True)
                        yield ResultsTable(id="results-table", zebra_stripes=True, cursor_type="cell")
                        yield Static("", id="results-status", markup=False)
                    with TabPane("SQL", id="tab-sql"):
                        yield TextArea("", read_only=True, soft_wrap=False, id="sql-text")
                    with TabPane("History", id="tab-history"):
                        yield DataTable(id="history-table", zebra_stripes=True, cursor_type="row")
            yield AIPanel(
                self.session, self._ai_target, self._apply_ai_document,
                lambda: self.editor.focus(), id="workspace-ai",
            )
        with Vertical(id="footer"):
            yield Static(KEY_HINTS, id="keymap")
            yield Static("", id="status", markup=False)

    def on_mount(self) -> None:
        self._refresh_header()
        self._refresh_tree()
        self.query_one("#history-table", DataTable).add_columns(
            "time", "source", "dialect", "rows", "ms", "document"
        )
        self._set_status(f"ready · {self.session.active_id} · {self.session.dialect}")
        if self.session.memory_error:
            self._set_status(self.session.memory_error)
            self.notify(self.session.memory_error, title="Saved data could not be loaded", severity="error", timeout=15)
        self.editor.focus()

    def on_unmount(self) -> None:
        self._closing = True
        if self.run_busy and self._run_task is None:
            self.run_busy = False
            self.session.busy = ""

    # -- widgets ------------------------------------------------------------

    @property
    def editor(self) -> TextArea:
        """The document editor."""
        return self.query_one("#editor", TextArea)

    @property
    def palette(self) -> CommandPalette:
        """The `\\` overlay."""
        return self.query_one("#palette", CommandPalette)

    @property
    def ai_panel(self) -> AIPanel:
        return self.query_one("#workspace-ai", AIPanel)

    def _ai_target(self) -> AITarget:
        return AITarget(
            (self.session.active_id, self._document_identity),
            self.session.active_id, self.editor.text,
        )

    def _apply_ai_document(self, proposal: AIProposal) -> None:
        self.editor.load_text(proposal.body)
        self.palette.close()
        self._set_status("AI replacement applied · not executed")

    @on(TextArea.Changed, "#editor")
    def _ai_document_changed(self) -> None:
        self.ai_panel.target_changed()

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
            with self.prevent(Select.Changed):
                select.value = self.session.dialect

    def _sync_source_select(self) -> None:
        select = self.query_one("#source-select", Select)
        if select.value != self.session.active_id:
            with self.prevent(Select.Changed):
                select.value = self.session.active_id

    def _refresh_tree(self) -> None:
        """Rebuild the explorer from the active source (columns load on expand)."""
        tree = self.query_one("#schema-tree", Tree)
        tree.clear()
        root = tree.root
        root.expand()
        datasets = self.session.source.datasets
        for name, entry in datasets.items():
            label = dataset_label(name, int(entry["rows"]))
            if entry.get("temp"):
                label.append(Text("  temp", style="dim italic"))
            node = root.add(
                label,
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

    def refuse_busy(self, action: str) -> bool:
        """Refuse shared-state work without blocking editor/navigation events."""
        if not self.session.busy:
            return False
        message = f"{action} unavailable · {self.session.busy}; wait for it to finish."
        self._set_status(message)
        self.notify(message, severity="warning")
        return True

    # -- actions ------------------------------------------------------------

    def action_run_or_chat(self) -> None:
        """The app's priority key sends chat when its input owns focus."""
        focused = self.screen.focused
        if focused is not None:
            for widget in (focused, *focused.ancestors):
                if isinstance(widget, AIPanel):
                    widget.action_send()
                    return
        self.action_run()

    def action_run(self) -> None:
        """Capture this submission and reserve the session before dispatching it."""
        if self._modal_open() or self.ai_panel.has_focus_within:
            return
        if self.refuse_busy("Run"):
            return
        document = self.editor.text
        self.palette.close()
        self.run_busy = True
        self.session.busy = "Query running"
        self._set_status(f"running · {self.session.active_id} · {self.session.dialect} · completion paused; editor and Settings remain available")
        self.run_worker(
            self._run_document(document), name="Run document", group="query",
            exit_on_error=False,
        )

    async def _run_document(self, document: str) -> None:
        self._run_task = asyncio.create_task(asyncio.to_thread(self.session.run, document))
        self._run_task.add_done_callback(self._run_finished)
        try:
            # Textual cancels workers on exit, not database writes. Keep the
            # real task alive; its callback releases ownership without touching
            # widgets after shutdown. No replacement run cancels this worker.
            await asyncio.shield(self._run_task)
        except Exception:
            pass  # _run_finished reports unexpected failures on the UI thread.

    def _run_finished(self, task: asyncio.Task[RunOutcome]) -> None:
        self._run_task = None
        self.run_busy = False
        self.session.busy = ""
        if task.cancelled():
            return
        try:
            outcome = task.result()
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            outcome = RunOutcome(status=message, error=message)
        if self._closing or not self.is_running:
            return
        self._set_error(outcome.error)
        self._render_results(outcome)
        self._refresh_history()
        # Rollback or a partially successful document may also change tables.
        self._refresh_tree()
        self._set_status(outcome.status)
        self._show_tab("tab-results")

    def action_compile(self) -> None:
        """Render the document as SQL for the active dialect — never executes."""
        if self._modal_open() or self.ai_panel.has_focus_within:
            return
        if self.refuse_busy("Compile"):
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
        if self.refuse_busy("Add data source"):
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

    def action_fn(self) -> None:
        """`\\fn` — open the function library."""
        self._open_fn()

    def action_query_to_fn(self) -> None:
        """Open a new function draft without saving or rewriting the document."""
        self._open_fn(new_body=self.editor.text)

    @on(Button.Pressed, "#query-to-fn")
    def _query_to_fn_clicked(self) -> None:
        self.action_query_to_fn()

    @on(Button.Pressed, "#results-copy")
    def _copy_results_clicked(self) -> None:
        self.query_one("#results-table", ResultsTable).action_copy_rows()

    @on(Button.Pressed, "#results-export")
    def _export_results_clicked(self) -> None:
        self.action_export_results()

    def action_export_results(self) -> None:
        if self._modal_open():
            return
        self.palette.close()
        self.query_one("#results-table", ResultsTable).action_export()

    def action_ai(self) -> None:
        """Open the in-layout assistant without changing the document."""
        if self._modal_open():
            return
        self.palette.close()
        self.ai_panel.open()

    def _open_fn(self, focus: str = "", new_name: str = "", new_body: str = "") -> None:
        """Open a saved function or a new draft with optional name and body."""
        if self._modal_open():
            return
        if self.refuse_busy("Function library"):
            return
        self.palette.close()
        self.push_screen(FnScreen(self, focus=focus, new_name=new_name, new_body=new_body))

    def _render_results(self, outcome: RunOutcome) -> None:
        table = self.query_one("#results-table", ResultsTable)
        table.show_result(outcome)
        self.query_one("#results-status", Static).update(outcome.status)


    def _source_added(self, source) -> None:
        """Register a source the modal built, then re-point the whole app at it."""
        if source is None:
            return
        self.session.register(source)
        self.ai_panel.target_changed()
        self.query_one("#source-select", Select).set_options(self._source_options())
        self._sync_source_select()
        self._sync_dialect_select()
        self._refresh_header()
        self._refresh_tree()
        self._set_status(f"{source.id} added · {len(source.datasets)} tables")

    # -- messages -----------------------------------------------------------


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
        if self.refuse_busy("Change data source"):
            self._sync_source_select()
            return
        self.session.set_active(source_id)
        self.ai_panel.target_changed()
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
        if self.refuse_busy("Change dialect"):
            self._sync_dialect_select()
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
        """Load a dataset's columns from the session snapshot, once."""
        node = event.node
        data = node.data if isinstance(node.data, dict) else {}
        if data.get("kind") != "dataset" or data.get("loaded"):
            return
        if self.refuse_busy("Load schema columns"):
            node.collapse()
            return
        node.remove_children()
        table = self.session.schema.table_by_name(data["name"])
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
        self._document_identity += 1
        self.editor.load_text(document)
        self.palette.close()
        self.editor.focus()
        self.ai_panel.target_changed()
        self._set_status("document loaded from history")


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
            "fn": self.action_fn,
            "query-to-fn": self.action_query_to_fn,
            "export-results": self.action_export_results,
            "ai": self.action_ai,
        }
        if event.action.startswith("fn-open:"):
            self._open_fn(focus=event.action[len("fn-open:") :])
            return
        if event.action.startswith("fn-new:"):
            self._open_fn(new_name=event.action[len("fn-new:") :])
            return
        action = actions.get(event.action)
        if action is not None:
            action()


def main() -> None:
    """`python -m anyql` — run the app."""
    AnyqlApp().run()


__all__ = ["AnyqlApp", "main"]
