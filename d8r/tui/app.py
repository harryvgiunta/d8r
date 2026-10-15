"""D8R's Textual application: editor, explorer, results — all in-process.

There is no webserver behind this app and none in front of it: the document is
parsed by `d8r.query`, executed by `d8r.engine`, and everything the widgets
show comes back as plain Python. The TUI is a *consumer* of those two layers —
it never re-implements a rule of the language or the engine.
"""

from __future__ import annotations

import asyncio
import json
from time import monotonic
from collections.abc import Callable
from copy import deepcopy
from uuid import uuid4

from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.content import Content, Text
from textual.screen import ModalScreen, Screen
from textual.widget import Widget
from textual.widgets import (
    Button,
    DataTable,
    Input,
    LoadingIndicator,
    OptionList,
    Select,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
    Tree,
)
from textual.widgets.option_list import Option
from textual.worker import Worker

from d8r.ai.context import AIProposal
from d8r.engine import DIALECTS, DIALECT_BY_NAME, DataSource
from d8r.query import ColumnDef
from d8r.themes import COZY_PASTEL

from .add_source import AddSourceModal
from .connection import CONNECTION_TIMEOUT, ConnectionProgress, build_saved_source, saved_source_label
from .ai import AIChats, AIPanel, AITarget, AgentsPane, DeleteOptionList
from .palette import CommandPalette, EditorPane
from .query_editor import QueryTextArea
from .page_details import PageDetails
from .results import ResultsTable
from .session import RunOutcome, Session
from .settings import SettingsScreen
from .fn import FnEditor, FnExplorer, HINT as FUNCTION_HINT
from .layout import PaneSplitter
from . import splash

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
        Binding("shift+tab", "previous_field", "Shift Tab", show=False),
        Binding("ctrl+c,super+c", "screen.copy_text", "Copy selected text", show=False),
    ]

    def action_noop(self) -> None:
        """The function form has fields; the query workspace never walks panes."""
        if self.app.function_mode:
            self.focus_next()

    def action_previous_field(self) -> None:
        if self.app.function_mode:
            self.focus_previous()




def column_label(column: ColumnDef) -> Text:
    """A column row in the schema explorer: the name, then its dtype."""
    return Text.assemble(column.name, "  ", (column.type, "dim"))


def dataset_label(name: str, rows: int | None) -> Text:
    """Remote schemas need not scan whole tables just to populate the explorer."""
    detail = "row count not loaded" if rows is None else f"{rows} rows"
    return Text.assemble((name, "bold"), "  ", (detail, "dim"))


class HistoryPreview(Static):
    """A bounded query reading surface, never an editor or a focus target."""

    can_focus = False

    def __init__(self, **kwargs) -> None:
        super().__init__("", markup=False, **kwargs)
        self.document: str | None = None
        self._rendered: tuple[str, int, int] | None = None
        self.border_title = "History · preview"

    def show_document(self, document: str, width: int, height: int) -> None:
        key = (document, width, height)
        if key == self._rendered:
            return
        self._rendered = key
        rows = max(1, height - 2)
        columns = max(1, width - 4)
        excerpt = document[:columns * rows]
        lines = Text(excerpt).wrap(self.app.console, columns, overflow="fold")
        self.update(Text("\n").join(lines[:rows]))
        clipped = len(excerpt) < len(document) or len(lines) > rows
        self.border_subtitle = "… more below" if clipped else "Read only"

    def on_mouse_down(self, event: events.MouseDown) -> None:
        # Like the palette, do not let a click focus a scrollable ancestor.
        self.app.editor.focus()
        event.stop()
        event.prevent_default()


class D8RApp(App):
    """The data IDE: a `\\command` document, the live schema, and the rows."""

    CSS_PATH = "app.tcss"
    TITLE = "D8R"
    SUB_TITLE = "data IDE"

    # Result tabs, left explorer and right-side assistant/agents tabs stay
    # mounted across editor modes.
    TAB_PANES: dict[str, str] = {
        "results": "tab-results",
        "sql": "tab-sql",
        "history": "tab-history",
    }
    PANES: dict[str, str] = {
        **TAB_PANES, "schema": "#schema-pane", "agents": "tab-agents", "ai": "#workspace-ai",
    }
    PANE_TITLES: dict[str, str] = {
        "results": "Results pane",
        "sql": "SQL pane",
        "history": "History pane",
        "schema": "Schema pane",
        "agents": "Agents tab",
        "ai": "AI pane (right)",
    }
    # Autosave after typing pauses; one timer absorbs the whole key-repeat burst.
    _WORKSPACE_SAVE_INTERVAL = 0.2

    # Priority bindings so they win over the focused editor's own keys — the
    # editor claims ctrl+k for "delete to line end" and the app needs it.
    BINDINGS = [
        Binding("ctrl+q", "back_or_quit", "Back / Quit", show=False),
        Binding("ctrl+j", "agents", "Agents", priority=True),
        Binding("ctrl+enter", "run_or_chat", "Run", priority=True),
        Binding("f5", "run", "Run", priority=True, show=False),
        Binding("ctrl+k", "compile", "Compile", priority=True),
        Binding("f6", "compile", "Compile", priority=True, show=False),
        Binding("ctrl+r", "preview_function", "Preview function", show=False),
        Binding("ctrl+d", "delete_function", "Delete function", show=False),
        Binding("ctrl+o", "add_source", "Data source", priority=True),
        # The way back into Settings when the Intellisense switch has the
        # palette shut — `\settings` itself needs the popup.
        Binding("ctrl+comma", "settings", "Settings", priority=True),
    ]

    def __init__(self, session: Session | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.session = session if session is not None else Session()
        self.register_theme(COZY_PASTEL)
        self.theme = self.session.theme
        self.ai_chats = AIChats(self, self.session)
        self._document_identity = self.session.workspace.get("document_id") or str(uuid4())
        self._workspace_key = self.session.source_key()
        document = self.session.workspace.get("document")
        self._initial_document = (WELCOME_DOCUMENT if self._workspace_key == "demo" else "") if document is None else document
        self._workspace_editor: TextArea | None = None
        self._workspace_timer = None
        self._workspace_edited = 0.0
        self._workspace_status: Static | None = None
        self._workspace_ready = False
        self._workspace_snapshot: dict = {}
        self._autosave_error = ""
        self.run_busy = False
        self._run_task: asyncio.Task[list[RunOutcome]] | None = None
        self._closing = False
        self.function_mode = False
        self._function_started = bool(self.session.workspace.get("function_draft"))
        self._page_rows: tuple = ()
        self._value_reads: dict[tuple[int, str, str, int], asyncio.Task] = {}
        self._connection_progress: ConnectionProgress | None = None
        self._connection_generation = 0
        self._startup_connection_started = False
        self._schema_workers: dict[str, tuple[DataSource, Worker]] = {}
        self._schema_errors: dict[str, str] = {}
        self._data_tabs: dict[str, dict] = {}
        self._data_tab_seq = 0
        self._results_closed = False
        self._main_outcome: RunOutcome | None = None
        self._workspace_outputs: dict[str, tuple[RunOutcome | None, str]] = {}

    def get_default_screen(self) -> Screen:
        """The IDE runs on `IdeScreen`, whose tab does not walk the panes."""
        return IdeScreen(id="_default")

    # -- layout -------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            with Vertical(id="explorer-slot"):
                with Horizontal(classes="pane-controls"):
                    yield Static("Explorer", classes="pane-title")
                    yield Button(Content("[x]"), id="explorer-close", classes="pane-close",
                                 compact=True, tooltip="Hide explorer · reopen with \\schema or \\pages")
                initial_tab = self.session.workspace["explorer_tab"]
                if initial_tab == "agents":
                    initial_tab = "schema"
                with TabbedContent(initial="tab-" + initial_tab, id="explorer-tabs"):
                    with TabPane("Schema", id="tab-schema"):
                        with Vertical(id="schema-pane"):
                            yield Static("Expand column: values · Enter: insert", id="schema-hint", markup=False)
                            with Horizontal(id="schema-index"):
                                yield LoadingIndicator(id="schema-index-dots")
                                yield Static("", id="schema-index-label", markup=False)
                            yield Tree(Text("datasets"), id="schema-tree")
                    with TabPane("Pages", id="tab-pages"):
                        with Horizontal(id="page-actions"):
                            yield Button("New", id="page-new", compact=True)
                            yield Button("Duplicate", id="page-duplicate", compact=True)
                            yield Button("Delete", id="page-delete", compact=True, disabled=True)
                        yield Input(placeholder="Search all pages…", id="page-search")
                        yield DeleteOptionList(id="pages-list", markup=False)
                        yield Static("↑/↓ select · Enter open · Delete remove", id="pages-hint", markup=False)
                    with TabPane("Functions", id="tab-functions"):
                        yield FnExplorer(id="fn-list-pane")
            yield PaneSplitter("#explorer-slot", "width", "explorer", min_size=16,
                               min_remaining=30, id="explorer-splitter")
            with Vertical(id="work-bench"):
                with Vertical(id="editor-slot"):
                    with Vertical(id="query-header"):
                        with Horizontal(id="query-targets"):
                            yield Select(
                                self._source_options(), allow_blank=False,
                                value=self.session.active_id, id="source-select",
                                tooltip="Switch source workspace",
                            )
                            yield Select(
                                self._dialect_options(), allow_blank=False,
                                value=self.session.dialect, id="dialect-select",
                                tooltip="SQL dialect for this query page",
                            )
                        with Horizontal(id="document-actions"):
                            yield Button("New Query", id="query-new", compact=True,
                                         tooltip="Create a blank query page; keep the current draft")
                            yield Button("Ask AI", id="open-ai", compact=True)
                            yield Button("Show Page Details", id="page-details-toggle", compact=True)
                            yield Button("Reconnect", id="reconnect-source", compact=True)
                        yield PageDetails(self, id="page-details")
                    with EditorPane(id="editor-pane"):
                        yield QueryTextArea(self._initial_document, show_line_numbers=True, id="editor")
                        yield CommandPalette(self.session, id="palette", markup=False)
                    yield FnEditor(self, id="function-editor")
                yield PaneSplitter("#editor-slot", "height", "editor", min_size=8,
                                   min_remaining=5, id="editor-splitter")
                with Vertical(id="output-slot"):
                    with Horizontal(id="output-controls", classes="pane-controls"):
                        yield Static("Query results", classes="pane-title")
                        yield Button(Content("[x]"), id="output-close", classes="pane-close",
                                     compact=True, tooltip="Hide results window · reopen with \\results, \\sql or \\history")
                    with TabbedContent(id="result-tabs"):
                        with TabPane("Results", id="tab-results"):
                            yield Static("", id="results-error", markup=False)
                            with Horizontal(id="results-actions"):
                                yield Button("Copy selection", id="results-copy", compact=True)
                                yield Button("Export CSV…", id="results-export", compact=True)
                                yield Button("Pin result", id="results-pin", compact=True)
                            yield ResultsTable(id="results-table", zebra_stripes=True, cursor_type="cell")
                            yield Static("", id="results-status", markup=False)
                        with TabPane("SQL", id="tab-sql"):
                            yield TextArea("", read_only=True, soft_wrap=False, id="sql-text")
                        with TabPane("History", id="tab-history"):
                            yield DataTable(id="history-table", zebra_stripes=True, cursor_type="row")
                    with Vertical(id="function-output"):
                        yield Static("Function preview · Run preview saves the definition", classes="pane-title")
                        yield DataTable(id="fn-grid", zebra_stripes=True, cursor_type="cell")
                yield HistoryPreview(id="history-preview")
            yield PaneSplitter("#assistant-slot", "width", "ai", reverse=True,
                               min_size=28, min_remaining=30, id="ai-splitter")
            with Vertical(id="assistant-slot"):
                with Horizontal(id="assistant-controls", classes="pane-controls"):
                    yield Static("AI / Agents", classes="pane-title")
                    yield Button(Content("[x]"), id="assistant-close", classes="pane-close",
                                 compact=True, tooltip="Hide AI and Agents pane · reopen with Ask AI or Ctrl+J")
                with TabbedContent(id="assistant-tabs"):
                    with TabPane("AI", id="tab-ai"):
                        yield AIPanel(
                            self.session, self._ai_target, self._apply_ai_document,
                            self._focus_editor, reveal=self._reveal_ai_chat, id="workspace-ai",
                        )
                    with TabPane("Agents", id="tab-agents"):
                        yield AgentsPane(id="workspace-agents")
        with Vertical(id="footer"):
            yield Static(KEY_HINTS, id="keymap")
            yield Static("", id="status", markup=False)

    def on_mount(self) -> None:
        self.query_one("#schema-tree", Tree).auto_expand = False
        for splitter in self.query(PaneSplitter):
            splitter.set_preferred_size(self.session.pane_sizes[splitter.setting_key])
        self._sync_layout()
        for name, visible in self.session.pane_visibility.items():
            if self.pane_visible(name) != visible:
                self.set_pane(name, visible)
        self._refresh_header()
        self._refresh_tree()
        self.query_one("#history-table", DataTable).add_columns(
            "time", "source", "dialect", "rows", "ms", "document"
        )
        self._refresh_history()
        self._workspace_editor = self.editor
        self._workspace_status = self.query_one("#status", Static)
        self.editor.move_cursor(tuple(self.session.workspace.get("cursor", [0, 0])))
        self._workspace_ready = True
        self.ai_panel.target_changed()
        self._set_status(f"ready · {self.session.active_id} · {self.session.dialect}")
        if self.session.memory_error:
            self._set_status(self.session.memory_error)
            self.notify(self.session.memory_error, title="Saved data could not be loaded", severity="error", timeout=15)
        self.editor.focus()
        self._queue_workspace_save()
        self._flush_workspace()
        self._refresh_pages()
        self.call_after_refresh(self._restore_startup_view)
        for source in self.session.sources.values():
            self._start_schema_index(source)

    def _restore_startup_view(self) -> None:
        """Restore the visible target before reconnecting it once, without running."""
        if self._closing or self._startup_connection_started:
            return
        self._startup_connection_started = True
        if self.session.workspace.get("active_view") == "function":
            self._open_fn(reveal_list=False)
        source_id = self.session.active_id
        if self.session.source_connected(source_id):
            return

        def connected(source: DataSource | None) -> None:
            self._source_added(source, activate=False)
            if source is not None:
                self._set_status(f"{source.id} connected · nothing executed")

        self.reconnect_source(source_id, connected, prompt_for_credentials=False)

    def on_unmount(self) -> None:
        self._closing = True
        self._cancel_connection()
        self._cancel_schema_indexes()
        self.session.clear_value_cache()
        self.ai_chats.shutdown()
        self._flush_workspace()
        if self.run_busy and self._run_task is None:
            self.run_busy = False
            self.session.busy = ""

    def exit(self, *args, **kwargs) -> None:
        """Flush live widget values even if their change messages are still queued."""
        self._cancel_connection()
        self._cancel_schema_indexes()
        self.session.clear_value_cache()
        if self._workspace_ready:
            self.function_editor.flush_draft()
        self._flush_workspace()
        self.ai_chats.shutdown()
        super().exit(*args, **kwargs)

    def save_workspace(self, **changes) -> bool:
        """Report autosave failures without discarding the in-memory document."""
        try:
            self.session.save_workspace(**changes)
        except ValueError as exc:
            message = str(exc)
            if not self._closing and self._workspace_status is not None and self._workspace_status.is_mounted:
                self._workspace_status.update(message)
                if message != self._autosave_error:
                    self.notify(message, title="Workspace could not be saved", severity="error", timeout=15)
            self._autosave_error = message
            return False
        self._autosave_error = ""
        if self._workspace_ready and not self._closing:
            self._refresh_pages()
        return True

    def _capture_workspace(self) -> None:
        if self._workspace_editor is not None:
            self._workspace_snapshot = {
                "document": self._workspace_editor.text,
                "document_id": self._document_identity,
                "cursor": list(self._workspace_editor.cursor_location),
                "source": self._workspace_key,
                "dialect": self.session.dialect,
            }

    def _queue_workspace_save(self) -> None:
        """Save after an idle interval, using one timer across a burst of edits."""
        if not self._workspace_ready or self._closing or not self.is_running:
            return
        self._workspace_edited = monotonic()
        if self._workspace_timer is None:
            self._workspace_timer = self.set_timer(
                self._WORKSPACE_SAVE_INTERVAL, self._save_idle_workspace)

    def _save_idle_workspace(self) -> None:
        self._workspace_timer = None
        remaining = self._WORKSPACE_SAVE_INTERVAL - (monotonic() - self._workspace_edited)
        if remaining > 0:
            self._workspace_timer = self.set_timer(max(0.01, remaining), self._save_idle_workspace)
        else:
            self._flush_workspace()

    def _flush_workspace(self) -> None:
        if self._workspace_timer is not None:
            self._workspace_timer.stop()
            self._workspace_timer = None
        self._capture_workspace()
        if self._workspace_snapshot:
            pages = self.session.workspace["pages"]
            try:
                details = self.query_one("#page-details", PageDetails)
                if details.page_id == self._document_identity:
                    values = details.values()
                    pages = [{**page, **values} if page["id"] == details.page_id else page
                             for page in pages]
            except NoMatches:
                pass  # Exit already flushed the fields before widgets unmounted.
            self.save_workspace(pages=pages, **self._workspace_snapshot)

    def _prepare_workspace_switch(self) -> None:
        """Capture outgoing widgets before their source or stable identities change."""
        self._workspace_outputs[self._workspace_key] = (
            self._main_outcome, self.query_one("#sql-text", TextArea).text)
        self.function_editor.flush_draft()
        self.ai_panel._flush_chat()
        self._flush_workspace()

    def _load_source_workspace(self, page: dict | None = None) -> None:
        """Restore this source's last page and function draft without executing."""
        self._workspace_key = self.session.source_key()
        with self.prevent(Select.Changed):
            self.query_one("#source-select", Select).set_options(self._source_options())
        state = self.session.workspace["source_workspaces"].get(self._workspace_key, {})
        pages = [item for item in self.session.workspace["pages"]
                 if item["source"] == self._workspace_key]
        if page is None:
            page = next((item for item in pages if item["id"] == state.get("document_id")),
                        pages[-1] if pages else None)
        if page is None:
            page = {"id": str(uuid4()), "document": "", "cursor": [0, 0],
                    "dialect": self.session.dialect}
        self._document_identity = page["id"]
        self.session.dialect = page["dialect"] or self.session.dialect
        self.editor.load_text(page["document"])
        self.editor.move_cursor(tuple(page["cursor"]))
        draft = state.get("function_draft")
        self._function_started = draft is not None
        self.function_editor.restore_workspace(draft)
        self.function_mode = state.get("active_view") == "function"
        self.palette.close()
        self._sync_source_select()
        self._sync_dialect_select()
        self._refresh_header()
        self._refresh_tree()
        self._refresh_history()
        outcome, sql = self._workspace_outputs.get(self._workspace_key, (None, ""))
        self._render_results(outcome or RunOutcome())
        self._set_error(outcome.error if outcome else "")
        self._set_sql(sql)
        self._sync_data_tabs()
        self._sync_layout()
        self.query_one("#keymap", Static).update(FUNCTION_HINT if self.function_mode else KEY_HINTS)
        self.ai_panel.target_changed()
        self._capture_workspace()
        self.save_workspace(function_draft=draft,
                            active_view="function" if self.function_mode else "workspace",
                            **self._workspace_snapshot)

    def _page_label(self, title: str, source: str, marker: str, width: int) -> Text:
        """A page row: exactly two lines, each cut to the explorer's width."""
        first = Text(f"{marker}{' '.join(title.split())}")
        first.truncate(width, overflow="ellipsis")
        second = Text(f"  {' '.join(source.split())}", style="dim")
        second.truncate(width, overflow="ellipsis")
        return Text("\n").join((first, second))

    def _refresh_pages(self) -> None:
        try:
            listing = self.query_one("#pages-list", OptionList)
        except NoMatches:
            # A teardown flush (a function draft saved from `on_unmount`) can
            # reach the explorer after its widgets were already pruned.
            return
        self.query_one("#page-details", PageDetails).sync()
        search = self.query_one("#page-search", Input).value.casefold().strip()
        pages = [page for page in self.session.workspace["pages"]
                 if page["source"] == self._workspace_key and
                 (not search or any(search in page.get(field, "").casefold()
                                    for field in ("title", "description", "document")))]
        rows = tuple((page["id"], page["title"], page.get("description", ""),
                      page["document"], page["source"]) for page in pages)
        # Options wrap by default, so a long title silently becomes a 4-line
        # row; the label is cut to the listing's own content width instead.
        # A hidden tab has no layout of its own: build with a floor and rebuild
        # for real when the tab activates (`_explorer_tab_changed`).
        width = max(8, listing.content_size.width - listing.styles.gutter.width
                    - listing.styles.scrollbar_size_vertical)
        state = (width, self._document_identity, rows)
        if state == self._page_rows:
            return
        self._page_rows = state
        highlighted = listing.highlighted
        highlighted_id = (listing.get_option_at_index(highlighted).id
                          if highlighted is not None and listing.option_count else None)
        listing.clear_options()
        for page in sorted(reversed(pages), key=lambda page: page["id"] != self._document_identity):
            title = page["title"] or next((line.strip() for line in page["document"].splitlines()
                                            if line.strip()), "Untitled query")
            marker = "• " if page["id"] == self._document_identity else ""
            listing.add_option(Option(self._page_label(title, page["source"], marker, width), id=page["id"]))
        if highlighted_id is not None and any(page["id"] == highlighted_id for page in pages):
            listing.highlighted = listing.get_option_index(highlighted_id)
        elif pages:
            listing.highlighted = min(highlighted or 0, len(pages) - 1)
        self.query_one("#page-delete", Button).disabled = listing.highlighted is None
        self.query_one("#pages-hint", Static).update(
            "No matching pages" if not pages and search else
            "↑/↓ select · Enter open · Delete remove")

    @on(Input.Changed, "#page-search")
    def _search_pages(self) -> None:
        if self._workspace_ready:
            self._flush_workspace()

    @on(Button.Pressed, "#page-details-toggle")
    def _toggle_page_details(self) -> None:
        details = self.query_one("#page-details", PageDetails)
        details.display = not details.display
        self.query_one("#page-details-toggle", Button).label = (
            "Hide Page Details" if details.display else "Show Page Details")

    @on(Button.Pressed, "#page-new")
    @on(Button.Pressed, "#query-new")
    def _new_page(self) -> None:
        if self.refuse_busy("New query page"):
            return
        self._flush_workspace()
        self._cancel_connection()
        self._load_document("")

    @on(Button.Pressed, "#page-duplicate")
    def _duplicate_page(self) -> None:
        if self.refuse_busy("Duplicate query page"):
            return
        self._flush_workspace()
        self._cancel_connection()
        self._load_document(self.editor.text)

    @on(OptionList.OptionSelected, "#pages-list")
    def _page_selected(self, event: OptionList.OptionSelected) -> None:
        self.open_page(str(event.option_id or ""))

    @on(OptionList.OptionHighlighted, "#pages-list")
    def _page_highlighted(self) -> None:
        self.query_one("#page-delete", Button).disabled = self.query_one("#pages-list", OptionList).highlighted is None

    @on(Button.Pressed, "#page-delete")
    def _delete_page_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.query_one("#pages-list", DeleteOptionList).action_delete_selected()

    @on(DeleteOptionList.DeleteRequested, "#pages-list")
    def _delete_page_requested(self, event: DeleteOptionList.DeleteRequested) -> None:
        event.stop()
        self.delete_page(event.option_id)

    def delete_page(self, page_id: str) -> None:
        pages = self.session.workspace["pages"]
        index = next((index for index, page in enumerate(pages)
                      if page["id"] == page_id and page["source"] == self._workspace_key), None)
        if index is None:
            return
        active = page_id == self._document_identity
        if active and self.session.busy != "AI context lookup running" and self.refuse_busy("Delete query page"):
            return
        self._flush_workspace()
        pages = self.session.workspace["pages"]
        remaining = [page for page in pages if page["id"] != page_id]
        for panel in self.ai_chats.panels():
            panel._flush_chat()
        chat_ids = [chat.id for chat in self.ai_chats.chats.values() if chat.page_id == page_id]
        self.ai_chats.discard(chat_ids)
        if active:
            candidates = [page for page in remaining if page["source"] == self._workspace_key]
            replacement = candidates[-1] if candidates else {
                "id": str(uuid4()), "title": "", "document": "", "cursor": [0, 0],
                "source": self.session.source_key(), "dialect": self.session.dialect,
            }
            self._restore_page(replacement)
        self._capture_workspace()
        removed_chats = set(chat_ids)
        saved = self.save_workspace(
            pages=remaining,
            chats={key: chat for key, chat in self.session.workspace["chats"].items() if key not in removed_chats},
            **self._workspace_snapshot,
        )
        self._refresh_pages()
        if saved:
            self._set_status("Query page deleted · nothing executed")
        else:
            self._set_status("Query page removed from this session, but deletion was not saved. " + self._autosave_error)

    def open_page(self, page_id: str) -> None:
        if self.refuse_busy("Open query page"):
            return
        if page_id == self._document_identity:
            if self.function_mode:
                self.action_workspace()
            return
        self._flush_workspace()
        page = next((item for item in self.session.workspace["pages"]
                     if item["id"] == page_id and item["source"] == self._workspace_key), None)
        if page is None:
            return
        self._restore_page(page)
        if self.function_mode:
            self.action_workspace()
        self.editor.focus()
        self._flush_workspace()
        if not self._autosave_error:
            self._set_status("Query page restored · nothing executed")

    def _restore_page(self, page: dict) -> None:
        """Swap the stable draft and its offline target without running or connecting."""
        self._cancel_connection()
        changed_source = page["source"] != self._workspace_key
        if changed_source:
            self._prepare_workspace_switch()
        self.session.restore_source(page["source"], page["dialect"])
        if changed_source:
            self._load_source_workspace(page)
        else:
            self._document_identity = page["id"]
            self.editor.load_text(page["document"])
            self.editor.move_cursor(tuple(page["cursor"]))
        with self.prevent(Select.Changed):
            self.query_one("#source-select", Select).set_options(self._source_options())
        self._sync_source_select()
        self._sync_dialect_select()
        self._refresh_header()
        self._refresh_tree()
        self.palette.close()
        self.ai_panel.target_changed()

    @on(TabbedContent.TabActivated, "#explorer-tabs")
    def _explorer_tab_changed(self, event: TabbedContent.TabActivated) -> None:
        if self._workspace_ready:
            self.save_workspace(explorer_tab=event.pane.id.removeprefix("tab-"))
            if event.pane.id == "tab-pages":
                # Labels built while the tab was hidden used the floor width.
                self.call_after_refresh(self._refresh_pages)

    def action_toggle_pages(self) -> None:
        """Reveal Pages from another tab; toggle the explorer if already selected."""
        if self._modal_open():
            return
        if self.function_mode:
            self.action_workspace()
        self.palette.close()
        tabs = self.query_one("#explorer-tabs", TabbedContent)
        visible = not (self.pane_visible("schema") and tabs.active == "tab-pages")
        if not self.update_settings(panes={"schema": visible}):
            return
        self.set_pane("schema", visible)
        tabs.active = "tab-pages"
        self.save_workspace(explorer_tab="pages")

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

    @property
    def function_editor(self) -> FnEditor:
        return self.query_one("#function-editor", FnEditor)

    def _focus_editor(self) -> None:
        if self.function_mode:
            self.function_editor.query_one("#fn-body", TextArea).focus()
        else:
            self.editor.focus()

    def _ai_target(self) -> AITarget:
        if self.function_mode:
            return self.function_editor._ai_target()
        return AITarget(
            (self.session.source_key(), self._document_identity),
            self.session.active_id, self.editor.text,
        )

    def _apply_ai_document(self, proposal: AIProposal) -> None:
        if self.function_mode:
            self.function_editor._apply_ai_draft(proposal)
            return
        if proposal.function is not None:
            fn = proposal.function
            self.session.save_fn(fn.name, ", ".join(fn.params), proposal.body, fn.doc)
            self.refresh_functions()
            self._set_status(f'Function "{fn.name}" saved locally · query unchanged and not executed')
            return
        if proposal.queries:
            if not self.pane_visible("schema") and not self.update_settings(panes={"schema": True}):
                raise ValueError("Pages could not be shown; resolve the settings error and retry.")
            pages = [page for page in self.session.workspace["pages"]
                     if page["id"] != self._document_identity]
            # Pages displays newest first. Keep the requested order and the
            # active page's stable identity; unrelated drafts are untouched.
            for index in range(len(proposal.queries) - 1, -1, -1):
                query = proposal.queries[index]
                pages.append({
                    "id": self._document_identity if index == 0 else str(uuid4()),
                    "title": query.title, "document": query.body, "cursor": [0, 0],
                    "source": self.session.source_key(), "dialect": self.session.dialect,
                })
            self.editor.load_text(proposal.queries[0].body)
            self.editor.move_cursor((0, 0))
            self.palette.close()
            self._capture_workspace()
            saved = self.save_workspace(pages=pages, explorer_tab="pages", **self._workspace_snapshot)
            if not saved:
                self._refresh_pages()
            self.query_one("#explorer-tabs", TabbedContent).active = "tab-pages"
            self.set_pane("schema", True)
            if saved:
                self._set_status("AI query drafts applied · nothing executed")
            return
        self.editor.load_text(proposal.body)
        self.palette.close()
        self._flush_workspace()
        self._set_status("AI replacement applied · not executed")

    def refresh_functions(self) -> None:
        """Refresh consumers after a saved definition changes, not editor text."""
        self.function_editor._draw_list()
        self.palette.sync(respect_dismissal=True)

    @on(TextArea.Changed, "#editor")
    def _ai_document_changed(self) -> None:
        if self._workspace_ready and self.is_running and not self._closing:
            self.ai_panel.target_changed()
            self._queue_workspace_save()

    @on(TextArea.SelectionChanged, "#editor")
    def _document_caret_changed(self) -> None:
        self._queue_workspace_save()

    def _source_options(self) -> list[tuple[Content, str | None]]:
        return [(Content("Add new Data Souce.."), None)] + [
            (Content(label), source_id)
            for label, source_id in sorted(self.session.source_options(), key=lambda option: option[0].casefold())
        ]

    def _dialect_options(self) -> list[tuple[Content, str]]:
        """Every advertised compile target; unavailable ones say so."""
        options: list[tuple[Content, str]] = []
        for spec in DIALECTS:
            label = spec["label"] if spec["compiles"] else f"{spec['label']} · requires live source"
            options.append((Content(label), spec["name"]))
        return options

    def _refresh_header(self) -> None:
        self.query_one("#reconnect-source", Button).display = not self.session.source_connected()

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
            label = dataset_label(name, entry["rows"])
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
        self._refresh_schema_status()

    def _refresh_schema_status(self) -> None:
        source = self.session.source
        pending = self._schema_workers.get(source.id)
        indexing = pending is not None and pending[0] is source
        error = self._schema_errors.get(source.id, "")
        self.query_one("#schema-index").display = indexing or bool(error)
        self.query_one("#schema-index-dots").display = indexing
        self.query_one("#schema-index-label", Static).update("Indexing schema…" if indexing else error)

    def _start_schema_index(self, source: DataSource) -> None:
        """Index only already-connected D1 sources; never reserve the query lane."""
        if self._closing or source.d1 is None or source.schema_indexed:
            return
        pending = self._schema_workers.get(source.id)
        if pending is not None:
            if pending[0] is source:
                return
            pending[1].cancel()
        self._schema_errors.pop(source.id, None)
        worker = self.run_worker(
            self._index_schema(source), name="Index D1 schema", group="schema-index", exit_on_error=False,
        )
        self._schema_workers[source.id] = (source, worker)
        self._refresh_schema_status()

    async def _index_schema(self, source: DataSource) -> None:
        error = ""
        try:
            schemas = await asyncio.wait_for(asyncio.to_thread(source.d1.schemas), CONNECTION_TIMEOUT)
            if not self._closing:
                self.session.sync_d1_schema(source, schemas)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            error = "Schema indexing timed out; Run still available"
        except Exception:
            error = "Schema indexing failed; Run still available"
        finally:
            pending = self._schema_workers.get(source.id)
            if pending is not None and pending[0] is source:
                self._schema_workers.pop(source.id)
                if not self._closing and self.session.sources.get(source.id) is source:
                    if error:
                        self._schema_errors[source.id] = error
                    if self.is_running:
                        self._schema_updated(source)

    def _schema_updated(self, source: DataSource) -> None:
        if self.session.source is source:
            self._refresh_tree()
        # Completion captures the fresh snapshot on its next editor event.
        # Do not synthesize a sync here: value suggestions can issue a query.

    def _cancel_schema_indexes(self) -> None:
        for _, worker in self._schema_workers.values():
            worker.cancel()
        self._schema_workers.clear()

    def _refresh_history(self) -> None:
        table = self.query_one("#history-table", DataTable)
        table.clear()
        for entry in self.session.history:
            table.add_row(
                entry.at,
                entry.source,
                entry.dialect,
                str(entry.rows),
                f"{entry.ms:.1f}",
                Text(entry.title),
            )

    # -- status -------------------------------------------------------------

    def _set_status(self, message: str) -> None:
        self.query_one("#status", Static).update(message)

    def update_settings(self, **changes) -> bool:
        """Persist before changing the workspace, reporting safe storage errors."""
        value_epoch = self.session.value_cache_epoch
        try:
            self.session.update_settings(**changes)
        except ValueError as exc:
            self._set_status(str(exc))
            self.notify(str(exc), title="Settings could not be saved", severity="error")
            return False
        if self.session.value_cache_epoch != value_epoch:
            self.value_cache_changed()
        if "theme" in changes:
            self.theme = self.session.theme
        return True


    def clear_value_cache(self) -> None:
        self.session.clear_value_cache()
        self.value_cache_changed()

    def value_cache_changed(self) -> None:
        """Discard visible old pools without starting another read from Settings."""
        for palette in self.query(CommandPalette):
            palette.reset_values()
        self._refresh_tree()

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

    @on(Button.Pressed, "#explorer-close")
    def _close_explorer(self) -> None:
        if self.update_settings(panes={"schema": False}):
            self.set_pane("schema", False)
            self._focus_editor()

    @on(Button.Pressed, "#output-close")
    def _close_output(self) -> None:
        if not self.update_settings(panes={name: False for name in self.TAB_PANES}):
            return
        self._results_closed = True
        for name in self.TAB_PANES:
            self.set_pane(name, False)
        self._focus_editor()

    @on(Button.Pressed, "#assistant-close")
    def _close_assistant(self) -> None:
        if not self.update_settings(panes={"ai": False, "agents": False}):
            return
        self.set_pane("ai", False)
        self.set_pane("agents", False)


    def pane_visible(self, name: str) -> bool:
        """Read tab visibility, the explorer preference or the live AI panel."""
        if name == "schema":
            return self.session.pane_visibility["schema"]
        if name == "ai":
            return self.ai_panel.display
        tabs = self.query_one("#assistant-tabs" if name == "agents" else "#result-tabs", TabbedContent)
        return tabs.get_tab(self.PANES[name]).display

    def set_pane(self, name: str, visible: bool) -> None:
        """Show or hide one pane; hiding the active tab moves to a visible one."""
        if name == "schema":
            self._sync_layout()
            return
        if name == "ai":
            if visible:
                self.ai_panel.open()
            else:
                self.ai_panel.action_close()
            self.call_after_refresh(self._refresh_splitters)
            return
        tabs = self.query_one("#assistant-tabs" if name == "agents" else "#result-tabs", TabbedContent)
        pane_id = self.PANES[name]
        if visible:
            if name in self.TAB_PANES:
                self._results_closed = False
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
        self._sync_layout()

    def _sync_layout(self) -> None:
        """Swap slot contents, never the slots or their user-selected dimensions."""
        self.query_one("#explorer-slot").display = self.session.pane_visibility["schema"]
        self.query_one("#fn-param-guide").display = self.function_mode
        self.query_one("#query-header").display = not self.function_mode
        self.query_one("#editor-pane").display = not self.function_mode
        self.function_editor.display = self.function_mode
        # Data tabs are part of the strip too: hiding Results, SQL and History
        # never throws away a result the user pinned or a run still holding.
        self.query_one("#result-tabs").display = not self.function_mode and (
            any(self.pane_visible(name) for name in self.TAB_PANES)
            or (any(tab["source"] == self._workspace_key for tab in self._data_tabs.values())
                and not self._results_closed)
        )
        self.query_one("#output-controls").display = self.query_one("#result-tabs").display
        self.query_one("#function-output").display = self.function_mode
        output = self.function_mode or self.query_one("#result-tabs").display
        self.query_one("#output-slot").display = output
        self.query_one("#editor-splitter").display = output
        if not output:
            self.query_one("#editor-slot").styles.height = "1fr"
        self.call_after_refresh(self._refresh_splitters)

    def _refresh_splitters(self) -> None:
        self.query_one("#explorer-splitter").display = self.query_one("#explorer-slot").display
        tabs = self.query_one("#assistant-tabs", TabbedContent)
        if tabs.get_tab("tab-ai").display != self.ai_panel.display:
            if self.ai_panel.display:
                tabs.show_tab("tab-ai")
            else:
                tabs.hide_tab("tab-ai")
        visible = self.ai_panel.display or self.pane_visible("agents")
        self.query_one("#assistant-slot").display = visible
        self.query_one("#ai-splitter").display = visible
        self._refresh_pages()
        for splitter in self.query(PaneSplitter):
            if splitter.display:
                splitter.refresh_size()

    @on(AIPanel.VisibilityChanged)
    def _ai_visibility_changed(self) -> None:
        if self._workspace_ready:
            self.call_after_refresh(self._refresh_splitters)

    def _reveal_ai_chat(self) -> None:
        self._refresh_splitters()
        self.query_one("#assistant-tabs", TabbedContent).active = "tab-ai"

    @on(TabbedContent.TabActivated, "#assistant-tabs")
    def _assistant_tab_changed(self, event: TabbedContent.TabActivated) -> None:
        if not self._workspace_ready or event.pane.id != event.tabbed_content.active:
            return
        if event.pane.id == "tab-agents":
            self.ai_panel._flush_chat()
            self.call_after_refresh(self.ai_chats.refresh_agents)
        elif (chat := self.ai_panel.chat) is not None and chat.unread:
            chat.unread = False
            self.ai_chats.persist(chat)
            self.ai_chats.changed(chat)

    def on_resize(self) -> None:
        if self._workspace_ready:
            self.call_after_refresh(self._refresh_splitters)
            self.call_after_refresh(self._place_history_preview)

    @on(PaneSplitter.Changed)
    def _pane_resized(self, event: PaneSplitter.Changed) -> None:
        if not self.update_settings(pane_sizes={event.setting_key: event.size}):
            event.splitter.set_preferred_size(self.session.pane_sizes[event.setting_key])
        self.call_after_refresh(self._refresh_pages)

    def action_back_or_quit(self) -> None:
        if self.function_mode and not self._modal_open():
            self.action_workspace()
        else:
            self.exit()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in {"preview_function", "delete_function"}:
            return self.function_mode and not self._modal_open() and not self.ai_panel.has_focus_within
        return super().check_action(action, parameters)

    def action_preview_function(self) -> None:
        self.function_editor.action_preview()

    def action_delete_function(self) -> None:
        self.function_editor.action_delete()

    def toggle_pane(self, name: str) -> bool | None:
        """Persist and flip one pane; return None if saving failed."""
        visible = not self.pane_visible(name)
        panes = {**self.session.pane_visibility, name: visible}
        if not self.update_settings(panes=panes):
            return None
        self.set_pane(name, visible)
        return visible

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
        if self.function_mode:
            self.function_editor.action_preview()
            return
        if self.refuse_busy("Run"):
            return
        document = self.editor.selected_text or self.editor.text
        if not self.session.source_connected():
            target = self.session.source_key()
            self.reconnect_source(
                self.session.active_id,
                lambda source: self._run_after_connect(source, document, target),
            )
            return
        self._start_run(document)

    def _run_after_connect(self, source: DataSource | None, document: str, target: str) -> None:
        if source is None:
            return
        self._source_added(source, index_schema=False)
        if self.session.source_key() != target:
            self._set_status("Source changed while reconnecting; press Run to query the new target.")
            self._start_schema_index(source)
            return
        self._start_run(document)

    def _start_run(self, document: str) -> None:
        if self.refuse_busy("Run"):
            return
        self.palette.close()
        self.run_busy = True
        self.session.busy = "Query running"
        self._set_status(f"running · {self.session.active_id} · {self.session.dialect} · completion paused; editor and Settings remain available")
        self.run_worker(
            self._run_document(document), name="Run document", group="query",
            exit_on_error=False,
        )

    async def _run_document(self, document: str) -> None:
        self._run_task = asyncio.create_task(asyncio.to_thread(self.session.run_all, document))
        source = self.session.source
        self._run_task.add_done_callback(lambda task: self._run_finished(task, source))
        try:
            # Textual cancels workers on exit, not database writes. Keep the
            # real task alive; its callback releases ownership without touching
            # widgets after shutdown. No replacement run cancels this worker.
            await asyncio.shield(self._run_task)
        except Exception:
            pass  # _run_finished reports unexpected failures on the UI thread.

    def _run_finished(self, task: asyncio.Task[list[RunOutcome]], source: DataSource) -> None:
        self._run_task = None
        self.run_busy = False
        self.session.busy = ""
        if task.cancelled():
            return
        try:
            outcomes = task.result()
        except Exception as exc:
            message = self.session.execution_error(exc, source=source)
            outcomes = [RunOutcome(status=message, error=message)]
        if self._closing or not self.is_running:
            return
        self.session.sync_d1_schema(source)
        outcome = outcomes[0]
        self._set_error(outcome.error)
        self._render_results(outcome)
        self._refresh_history()
        # Rollback or a partially successful document may also change tables.
        self._schema_updated(source)
        status = outcome.status
        if len(outcomes) > 1:
            # A multi-query document answers on its own tabs; name the extras.
            status += f" · {len(outcomes)} results"
        if outcomes[-1].error and len(outcomes) > 1:
            status = outcomes[-1].status
        self._set_status(status)
        if self.session.workspace_error:
            self.notify(self.session.workspace_error, title="History could not be saved", severity="error", timeout=15)
        self._show_tab("tab-results")
        self.call_next(self._open_data_tabs, outcomes[1:])
        # A reconnect-for-Run gives the first query priority over full discovery.
        # Subsequent runs never restart a failed index implicitly.
        if source.id not in self._schema_errors:
            self._start_schema_index(source)

    def action_compile(self) -> None:
        """Render the document as SQL for the active dialect — never executes."""
        if self.function_mode or self._modal_open() or self.ai_panel.has_focus_within:
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
        snapshot_path = self.session.saved_snapshot_path(self.session.active_id) if not self.session.source_connected() else None
        self.push_screen(AddSourceModal(self.session, snapshot_path=snapshot_path), self._source_added)

    @on(Button.Pressed, "#reconnect-source")
    def _reconnect_active_source(self) -> None:
        if not self._modal_open() and not self.session.source_connected():
            self.reconnect_source(self.session.active_id, self._source_added)

    def reconnect_source(
        self, source_id: str, connected: Callable[[DataSource | None], None],
        *, prompt_for_credentials: bool = True,
    ) -> None:
        """Reconnect a saved target with bounded progress and stale-target guards."""
        self._sync_source_select()
        self._cancel_connection()
        profile = self.session.saved_source_profile(source_id)
        snapshot_path = self.session.saved_snapshot_path(source_id)
        if self.refuse_busy("Connect data source"):
            return
        self.palette.close()
        missing_credentials = (profile is not None and "account_id" in profile
                               and not profile.get("api_token"))
        if (profile is None and snapshot_path is None) or missing_credentials:
            self._set_status("This source is disconnected. Add its connection details to reconnect.")
            if prompt_for_credentials:
                self.push_screen(AddSourceModal(self.session, profile=profile, snapshot_path=snapshot_path), connected)
            return
        profile = deepcopy(profile) if profile is not None else None
        generation = self._connection_generation
        source = self.session.source
        target_source = self.session.sources.get(source_id)
        screen = self.screen

        def valid() -> bool:
            return (not self._closing and generation == self._connection_generation
                    and self.session.source is source and screen.is_mounted
                    and self.session.sources.get(source_id) is target_source)

        def completed(built: DataSource) -> None:
            self._connection_progress = None
            connected(built)

        progress = ConnectionProgress(
            saved_source_label(profile=profile, snapshot_path=snapshot_path),
            lambda: build_saved_source(self.session, profile=profile, snapshot_path=snapshot_path),
            completed, valid=valid, failed=self._set_status,
        )
        self._connection_progress = progress
        screen.mount(progress)

    def _cancel_connection(self) -> None:
        self._connection_generation += 1
        if self._connection_progress is not None:
            self._connection_progress.cancel()
            self._connection_progress = None

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
        if visible is not None:
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
        self._open_fn(new_body=self.editor.text, restore_draft=False)

    @on(Button.Pressed, "#query-to-fn")
    def _query_to_fn_clicked(self) -> None:
        self.action_query_to_fn()

    @on(Button.Pressed, "#results-copy")
    def _copy_results_clicked(self) -> None:
        self.query_one("#results-table", ResultsTable).action_copy_selection()

    @on(Button.Pressed, "#results-export")
    def _export_results_clicked(self) -> None:
        self.action_export_results()

    def action_export_results(self) -> None:
        if self._modal_open():
            return
        self.palette.close()
        self._active_results_table().action_export()

    @on(Button.Pressed, "#open-ai")
    def action_ai(self) -> None:
        """Open the in-layout assistant without changing the document."""
        if self._modal_open():
            return
        self.palette.close()
        self.ai_panel.open()
        self.call_after_refresh(self._refresh_splitters)

    def action_agents(self) -> None:
        """Reveal and focus the right tab without changing the active editor."""
        if self._modal_open():
            return
        for panel in self.ai_chats.panels():
            panel._flush_chat()
        panes = {"agents": True}
        if any(not self.session.pane_visibility[name] for name in panes):
            if not self.update_settings(panes=panes):
                return
        self.palette.close()
        self.set_pane("agents", True)
        self._refresh_splitters()
        self.query_one("#agents-list", OptionList).focus()

    def open_ai_chat(self, chat_id: str | None) -> None:
        chat = self.ai_chats.chats.get(chat_id)
        if chat is None:
            return
        try:
            function_mode = json.loads(chat.target_key)[0] == "function"
        except (ValueError, TypeError, IndexError):
            function_mode = False
        if self._modal_open():
            return
        if chat.source_key != self._workspace_key:
            return
        if function_mode:
            self._open_fn(reveal_list=False)
        else:
            self.action_workspace()
        self.ai_panel.select_chat(chat.id)
        self.call_after_refresh(self._refresh_splitters)

    def _open_fn(self, focus: str = "", new_name: str = "", new_body: str = "", *,
                 restore_draft: bool = True, reveal_list: bool = True) -> None:
        """Open the function editor while keeping the workspace mounted."""
        if self._modal_open() or self.refuse_busy("Function library"):
            return
        if reveal_list and not self.session.pane_visibility["schema"]:
            if not self.update_settings(panes={"schema": True}):
                return
        self._cancel_connection()
        self.palette.close()
        self.ai_panel._flush_chat()
        self._flush_workspace()
        if not self._function_started or not restore_draft or focus or new_name or new_body:
            self.function_editor.start_draft(focus=focus, new_name=new_name, new_body=new_body)
        self._function_started = True
        self.function_editor._queue_draft_save()
        self.function_editor._draw_list()
        self.function_mode = True
        self._sync_layout()
        if reveal_list:
            self.query_one("#explorer-tabs", TabbedContent).active = "tab-functions"
        self.ai_panel.target_changed()
        self.save_workspace(active_view="function")
        self.query_one("#keymap", Static).update(FUNCTION_HINT)
        self.function_editor.query_one("#fn-name").focus()

    def action_workspace(self) -> None:
        """Return to the unchanged query, preserving draft, chat and pane sizes."""
        if self._modal_open():
            return
        self._cancel_connection()
        self.function_editor.flush_draft()
        self.function_editor.query_one(CommandPalette).close()
        self.ai_panel._flush_chat()
        self.function_mode = False
        self._sync_layout()
        self.ai_panel.target_changed()
        self.save_workspace(active_view="workspace")
        self.query_one("#keymap", Static).update(KEY_HINTS)
        self.editor.focus()
        self.palette.sync(respect_dismissal=True)

    @on(OptionList.OptionSelected, "#fn-list")
    def _function_selected(self, event: OptionList.OptionSelected) -> None:
        self.function_editor.select_function(str(event.option_id or ""))

    def _render_results(self, outcome: RunOutcome) -> None:
        self._main_outcome = outcome
        table = self.query_one("#results-table", ResultsTable)
        table.show_result(outcome)
        self.query_one("#results-status", Static).update(outcome.status)

    # -- data tabs ------------------------------------------------------------

    def _open_data_tabs(self, outcomes: list[RunOutcome]) -> None:
        """One extra tab per result beyond the first, expanding out from History.

        A run replaces the previous run's unpinned tabs; a pinned tab keeps
        its rows across every later run until the user closes it.
        """
        for tab_id in [key for key, tab in self._data_tabs.items()
                       if tab["source"] == self._workspace_key and not tab["pinned"]]:
            self._remove_data_tab(tab_id)
        first_new = ""
        for outcome in outcomes:
            first_new = first_new or self._append_data_tab(outcome)
        if first_new:
            # The newest result comes forward; older and pinned tabs stay put.
            self.query_one("#result-tabs", TabbedContent).active = first_new

    def _append_data_tab(self, outcome: RunOutcome, *, pinned: bool = False) -> str:
        """Build one result tab at the far end, past History and its siblings."""
        self._data_tab_seq += 1
        tab_id = f"tab-data-{self._data_tab_seq}"
        label = self._data_tab_label(tab_id, pinned=pinned)
        table = ResultsTable(id=f"data-{tab_id}", classes="data-table", zebra_stripes=True, cursor_type="cell")
        pane = TabPane(
            label,
            Horizontal(
                Button("Unpin" if pinned else "Pin", classes="data-pin", compact=True),
                Button("Export CSV…", classes="data-export", compact=True),
                Button("Close", classes="data-close", compact=True),
                classes="data-actions",
            ),
            table,
            Static(outcome.status, classes="data-status", markup=False),
            id=tab_id,
        )
        tabs = self.query_one("#result-tabs", TabbedContent)
        last = next(reversed(self._data_tabs), "")
        self._data_tabs[tab_id] = {"pinned": pinned, "label": label, "source": self._workspace_key}
        tabs.add_pane(pane, after=last or "tab-history")
        self.call_after_refresh(table.show_result, outcome)
        self.call_after_refresh(self._sync_data_tabs)
        return tab_id

    def _sync_data_tabs(self) -> None:
        tabs = self.query_one("#result-tabs", TabbedContent)
        for tab_id, tab in self._data_tabs.items():
            if tab["source"] == self._workspace_key:
                tabs.show_tab(tab_id)
            else:
                tabs.hide_tab(tab_id)

    @on(Button.Pressed, "#results-pin")
    def _pin_main_result(self) -> None:
        """Keep the current Results content through every later run."""
        outcome = self._main_outcome
        if outcome is None or not outcome.columns:
            self.notify("Nothing to pin yet", severity="warning")
            return
        tab_id = self._append_data_tab(outcome, pinned=True)
        self.query_one("#result-tabs", TabbedContent).active = tab_id
        self._set_status("Result pinned · it survives the next run")

    @staticmethod
    def _data_tab_label(tab_id: str, *, pinned: bool) -> str:
        number = tab_id.rsplit("-", 1)[-1]
        return f"◆ Data {number}" if pinned else f"Data {number}"

    def _remove_data_tab(self, tab_id: str) -> None:
        self._data_tabs.pop(tab_id, None)
        tabs = self.query_one("#result-tabs", TabbedContent)
        was_active = tabs.active == tab_id
        tabs.remove_pane(tab_id)
        if was_active:
            # remove_pane auto-activates a neighbour; a closed result always
            # hands the view back to the run's main pane, once the removal lands.
            self.call_after_refresh(setattr, tabs, "active", "tab-results")

    @on(Button.Pressed, ".data-pin")
    def _data_pin_clicked(self, event: Button.Pressed) -> None:
        tab_id = self._tab_id_for(event.button)
        tab = self._data_tabs[tab_id]
        tab["pinned"] = not tab["pinned"]
        event.button.label = "Unpin" if tab["pinned"] else "Pin"
        tabs = self.query_one("#result-tabs", TabbedContent)
        tabs.get_tab(tab_id).label = self._data_tab_label(tab_id, pinned=tab["pinned"])

    @on(Button.Pressed, ".data-close")
    def _data_close_clicked(self, event: Button.Pressed) -> None:
        self._remove_data_tab(self._tab_id_for(event.button))

    @on(Button.Pressed, ".data-export")
    def _data_export_clicked(self, event: Button.Pressed) -> None:
        tab_id = self._tab_id_for(event.button)
        self.query_one(f"#data-{tab_id}", ResultsTable).action_export()

    @staticmethod
    def _tab_id_for(widget: Widget) -> str:
        return next(pane.id for pane in widget.ancestors if isinstance(pane, TabPane) and pane.id)

    def _active_results_table(self) -> ResultsTable:
        """The grid the active tab shows: the run's own, or a pinned data tab."""
        tabs = self.query_one("#result-tabs", TabbedContent)
        if tabs.active in self._data_tabs:
            return self.query_one(f"#data-{tabs.active}", ResultsTable)
        return self.query_one("#results-table", ResultsTable)


    def _source_added(self, source, *, activate: bool = True, index_schema: bool = True) -> None:
        """Register a built source; function targets need not switch the workspace."""
        if source is None:
            return
        self.session.register(source, activate=False)
        self.function_editor.retarget(source)
        with self.prevent(Select.Changed):
            self.query_one("#source-select", Select).set_options(self._source_options())
        self._sync_source_select()
        self._refresh_header()
        self._refresh_tree()
        self._sync_dialect_select()
        self.ai_panel.target_changed()
        self._queue_workspace_save()
        self._schema_errors.pop(source.id, None)
        if index_schema:
            self._start_schema_index(source)
        if activate and self.select_source(source.id):
            detail = f"{len(source.datasets)} tables" if source.schema_indexed else "ready"
            self._set_status(f"{source.id} added · {detail}")
        if isinstance(self.screen, SettingsScreen):
            self.screen._draw()

    # -- messages -----------------------------------------------------------


    @on(Select.Changed, "#source-select")
    def _source_changed(self, event: Select.Changed) -> None:
        if not self._workspace_ready or event.value != event.select.value:
            return
        if event.value is None:
            self._sync_source_select()
            self.action_add_source()
        elif isinstance(event.value, str) and event.value != self.session.active_id:
            self.select_source(event.value)

    def select_source(self, source_id: str, *, open_function: bool = False) -> bool:
        """Select the offline workspace first; reconnect without delaying navigation."""
        key = self.session.source_key(source_id)
        if key != self._workspace_key:
            if self.refuse_busy("Change data source"):
                self._sync_source_select()
                return False
            self._cancel_connection()
            self._prepare_workspace_switch()
            source_id = self.session.restore_source(key, activate=False)
            if not self.update_settings(source=source_id):
                self._sync_source_select()
                return False
            self._load_source_workspace()
            self._set_status(f"{self.session.active_id} workspace restored · nothing executed")
        if open_function:
            self._open_fn()
        if not self.session.source_connected(source_id):
            self.reconnect_source(source_id, self._source_added)
        else:
            self._cancel_connection()
        return True

    @on(Select.Changed, "#dialect-select")
    def _dialect_changed(self, event: Select.Changed) -> None:
        if isinstance(event.value, str) and event.value == event.select.value:
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
        if not self.update_settings(dialect=name):
            self._sync_dialect_select()
            return
        self._sync_dialect_select()
        self._queue_workspace_save()
        spec = DIALECT_BY_NAME.get(name)
        if (spec is not None and not spec["compiles"]
                and not (self.session.source.kind == "ibis" and self.session.source.dialect == name)):
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
            if data.get("kind") == "column" and not data.get("loaded"):
                self._inspect_column(node)
            return
        node.remove_children()
        table = self.session.schema.table_by_name(data["name"])
        if table is None:
            node.add_leaf(Text("not in the registry", style="dim"), data={"kind": "note"})
        else:
            for column in table.columns:
                child = node.add(column_label(column), data={"kind": "column", "name": column.name,
                                                           "dataset": data["name"]}, allow_expand=True)
                child.add_leaf(Text("Expand to load distinct values", style="dim"), data={"kind": "placeholder"})
        data["loaded"] = True

    def _inspect_column(self, node) -> None:
        source = self.session.source
        data = node.data
        if not self.session.source_connected():
            self._set_status("Reconnect this source before inspecting values.")
            node.collapse()
            return
        cached = self.session.cached_values(source, data["dataset"], data["name"])
        if cached is not None:
            self._render_distinct_values(node, *cached)
            return
        node.remove_children()
        node.add_leaf(Text("Loading distinct values…", style="dim"), data={"kind": "note"})
        self.run_worker(
            self._read_distinct_values(source, node, data, self.session.value_cache_epoch),
            group="schema-values", exit_on_error=False,
        )

    async def _column_values(self, source: DataSource, dataset: str, column: str) -> tuple[list[object], bool]:
        """One in-flight request per source/column, shared by tree and completion."""
        if self._closing or self.session.sources.get(source.id) is not source:
            raise RuntimeError("The value source has changed.")
        cached = self.session.cached_values(source, dataset, column)
        if cached is not None:
            return cached
        key = (id(source), dataset, column, self.session.value_cache_epoch)
        task = self._value_reads.get(key)
        if task is None:
            # Local backends share one transaction-owning connection. D1's
            # independent HTTP reads do not reserve or block the query lane.
            reserve = source.d1 is None
            if reserve and self.session.busy:
                raise RuntimeError("The local connection is busy; try again after it finishes.")
            if reserve:
                self.session.busy = "Loading distinct values"
            task = asyncio.create_task(asyncio.to_thread(
                self.session.distinct_values, source, dataset, column, epoch=key[-1],
            ))
            self._value_reads[key] = task
            task.add_done_callback(lambda done: self._value_read_finished(key, done, reserve))
        return await asyncio.shield(task)

    def _value_read_finished(self, key, task, reserved: bool) -> None:
        if self._value_reads.get(key) is task:
            self._value_reads.pop(key)
        if reserved:
            self.session.busy = ""
        if not task.cancelled():
            task.exception()  # Also consume failures after a consumer or the app has closed.

    @on(CommandPalette.HistoryPreviewChanged)
    def _history_preview_changed(self, event: CommandPalette.HistoryPreviewChanged) -> None:
        if event.palette is not self.palette:
            return
        event.stop()
        preview = self.query_one("#history-preview", HistoryPreview)
        preview.document = event.document
        if event.document is None:
            preview.display = False
        else:
            self._place_history_preview()

    def _place_history_preview(self) -> None:
        preview = self.query_one("#history-preview", HistoryPreview)
        palette = self.palette
        pane = palette.parent
        if (preview.document is None or self.function_mode or not palette.is_open
                or not palette.visible or pane is None or preview.parent is None):
            preview.display = False
            return
        bounds = preview.parent.content_region.intersection(self.screen.region)
        offset = palette.styles.offset
        left = pane.content_region.x + int(offset.x.value)
        top = pane.content_region.y + int(offset.y.value)
        palette_width = int(palette.styles.width.value)
        palette_height = int(palette.styles.height.value)
        right = left + palette_width
        bottom = top + palette_height
        right_space = bounds.right - right - 1
        left_space = left - bounds.x - 1
        if max(right_space, left_space) >= 26:
            on_right = right_space >= left_space
            width = min(68, right_space if on_right else left_space)
            height = min(12, bounds.height)
            x = right + 1 if on_right else left - width - 1
            y = max(bounds.y, min(top, bounds.bottom - height))
        else:
            # The workbench overlay can extend over results on small terminals;
            # keeping it outside the offer list preserves both reading surfaces.
            caret_y = self.editor.cursor_screen_offset.y
            below_top = max(bottom, caret_y + 1)
            above_bottom = min(top, caret_y)
            below = bounds.bottom - below_top
            above = above_bottom - bounds.y
            place_below = below >= above
            width = min(68, bounds.width)
            height = min(10, below if place_below else above)
            x = max(bounds.x, min(left, bounds.right - width))
            y = below_top if place_below else above_bottom - height
        if width < 8 or height < 4:
            preview.display = False
            return
        preview.styles.offset = (x - preview.parent.content_region.x,
                                 y - preview.parent.content_region.y)
        preview.styles.width = width
        preview.styles.height = height
        preview.show_document(preview.document, width, height)
        preview.display = True

    @on(CommandPalette.ValuesRequested)
    def _palette_values_requested(self, event: CommandPalette.ValuesRequested) -> None:
        event.stop()
        self.run_worker(self._complete_palette_values(event), group="completion-values", exit_on_error=False)

    async def _complete_palette_values(self, event: CommandPalette.ValuesRequested) -> None:
        if not event.palette.needs_values(event.key):
            return
        try:
            values, _ = await self._column_values(event.source, event.dataset, event.column)
        except asyncio.CancelledError:
            raise
        except Exception:
            values = []
        if not self._closing and self.session.sources.get(event.source.id) is event.source:
            event.palette.values_received(event.key, values)

    async def _read_distinct_values(self, source, node, data: dict, epoch: int) -> None:
        try:
            values, capped = await self._column_values(source, data["dataset"], data["name"])
        except asyncio.CancelledError:
            raise
        except Exception:
            if (not self._closing and self.session.source is source
                    and epoch == self.session.value_cache_epoch):
                node.remove_children()
                node.add_leaf(Text("Could not load values; collapse and expand to retry", style="dim"),
                              data={"kind": "note"})
            return
        if (self._closing or self.session.source is not source
                or epoch != self.session.value_cache_epoch):
            return
        self._render_distinct_values(node, values, capped)

    def _render_distinct_values(self, node, values: list[object], capped: bool) -> None:
        node.remove_children()
        for value in values:
            label = Text("NULL", style="dim italic") if value is None else Text(repr(value) if isinstance(value, str) else str(value))
            node.add_leaf(label, data={"kind": "value"})
        note = f"{len(values)} distinct values" + (" shown · more available" if capped else "")
        node.add_leaf(Text(note, style="dim"), data={"kind": "note"})
        node.data["loaded"] = self.session.value_cache_enabled

    @on(Tree.NodeSelected, "#schema-tree")
    def _column_selected(self, event: Tree.NodeSelected) -> None:
        """Insert a selected column using its table's current document alias."""
        data = event.node.data if isinstance(event.node.data, dict) else {}
        editor = self.function_editor.query_one("#fn-body", TextArea) if self.function_mode else self.editor
        palette = self.function_editor.query_one(CommandPalette) if self.function_mode else self.palette
        if data.get("kind") == "dataset":
            palette.sync(respect_dismissal=True)
            view = palette.view
            row, _ = editor.cursor_location
            head = editor.document[row].split(maxsplit=1)
            if (view is not None and view.phase == "argument" and head
                    and head[0].lower() in {"\\from", "\\open", "\\join"}):
                index = next((index for index, entry in enumerate(view.entries) if entry.label == data["name"]), None)
                if index is not None and palette.accept(index):
                    editor.focus()
                    self._set_status(f"inserted {data['name']} with alias")
                    return
            event.node.toggle()
            return
        if event.node.is_root:
            event.node.toggle()
            return
        if data.get("kind") != "column":
            return
        reference = self.session.column_reference(editor.text, data["dataset"], data["name"])
        editor.insert(reference)
        editor.focus()
        palette.sync()
        self._set_status(f"inserted {reference}")

    @on(DataTable.RowSelected, "#history-table")
    def _history_selected(self, event: DataTable.RowSelected) -> None:
        """Restore the recorded document and target without executing or connecting."""
        if 0 <= event.cursor_row < len(self.session.history):
            if self.refuse_busy("Load history"):
                return
            entry = self.session.history[event.cursor_row]
            self._prepare_workspace_switch()
            self._cancel_connection()
            self.session.restore_source(entry.target or entry.source, entry.dialect)
            if self.session.source_key() != self._workspace_key:
                self._load_source_workspace()
            with self.prevent(Select.Changed):
                self.query_one("#source-select", Select).set_options(self._source_options())
            self._sync_source_select()
            self._sync_dialect_select()
            self._refresh_header()
            self._refresh_tree()
            self._load_document(entry.doc)

    def _load_document(self, document: str) -> None:
        self._document_identity = str(uuid4())
        self.editor.load_text(document)
        self.palette.close()
        self.editor.focus()
        self.ai_panel.target_changed()
        self._set_status("Query page opened · nothing executed")
        self._flush_workspace()


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
            "toggle_pages": self.action_toggle_pages,
            "settings": self.action_settings,
            "fn": self.action_fn,
            "query-to-fn": self.action_query_to_fn,
            "export-results": self.action_export_results,
            "ai": self.action_ai,
        }
        if event.action.startswith("fn-new:"):
            self._open_fn(new_name=event.action[len("fn-new:") :], restore_draft=False)
            return
        action = actions.get(event.action)
        if action is not None:
            action()


def main() -> None:
    """`python -m d8r` — optionally show the entry screen, then run one session."""
    session = Session()
    if session.entry_screen and splash.should_play():
        splash.play()
    D8RApp(session).run()


__all__ = ["D8RApp", "main"]
