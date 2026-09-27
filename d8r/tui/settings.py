"""Settings workspace: persistent sidebar navigation and central category details.

Rows read live state whenever a category is drawn. Changes apply to the IDE
immediately; connection and provider forms retain their explicit Add/Save steps.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option

from d8r.ai.client import AIConfig
from d8r.ai.config import import_yolo_config

from d8r.engine import DIALECT_BY_NAME, DIALECTS

from .add_source import AddSourceModal
from .palette import prompt

if TYPE_CHECKING:
    from .app import D8RApp

# Sidebar order is also the keyboard navigation order.
MENUS: dict[str, str] = {
    "general": "General",
    "menus": "Show/Hide Menus",
    "sources": "Data source",
    "dialects": "Dialect",
    "ai": "AI provider",
    "keys": "Keybindings",
}

@dataclass(frozen=True)
class Row:
    """One menu row: what it reads as, and what selecting it does."""

    label: str
    detail: str = ""
    # pane | intellisense | default-rows | source | dialect | ai | add-source.
    action: str = ""
    value: str = ""


def binding_rows() -> list[Row]:
    """Every key the IDE answers to, read from the classes that declare them.

    Nothing is retyped here: each row is the `Binding` the widget or the app
    actually carries, so the reference cannot drift from the keymap.
    """
    from textual.app import App as TextualApp

    from .app import D8RApp
    from .results import ResultsTable
    from .palette import EditorPane

    groups = (
        ("app", D8RApp.BINDINGS),
        ("document", EditorPane.BINDINGS),
        ("results", ResultsTable.BINDINGS),
        ("settings", SettingsScreen.BINDINGS),
        ("add source", AddSourceModal.BINDINGS),
        # Only what the framework still answers to: `IdeScreen` drops Textual's
        # tab/shift+tab focus keys, so listing them would be a lie.
        ("textual", TextualApp.BINDINGS),
    )
    rows: list[Row] = []
    for group, bindings in groups:
        for binding in bindings:
            key = str(binding.key)
            # Multi-key aliases (`ctrl+c,super+c`) are for terminals, not people.
            if not binding.description or "," in key:
                continue
            rows.append(Row(key, f"{binding.description} · {group}"))
    return rows


class SettingsScreen(ModalScreen[None]):
    """The `\\settings` menu; every change lands in the IDE behind it at once."""

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("left", "sidebar", "Categories", show=False),
        Binding("right", "details", "Details", show=False),
    ]

    def __init__(self, ide: "D8RApp", **kwargs) -> None:
        super().__init__(**kwargs)
        self.ide = ide
        self.menu = "general"
        self.rows: list[Row] = []

    @property
    def session(self):
        """The session whose state the menu reads and writes."""
        return self.ide.session

    def compose(self) -> ComposeResult:
        with Vertical(id="settings"):
            yield Static("Settings", id="settings-title")
            with Horizontal(id="settings-workspace"):
                with Vertical(id="settings-sidebar-pane"):
                    yield Static("CATEGORIES", id="settings-nav-title")
                    yield OptionList(
                        *(Option(label, id=name) for name, label in MENUS.items()),
                        id="settings-sidebar",
                    )
                with Vertical(id="settings-content"):
                    yield Static("", id="settings-heading")
                    yield Static("", id="settings-description", markup=False)
                    yield OptionList(id="settings-menu")
            yield Static("", id="settings-hint")

    def on_mount(self) -> None:
        self.query_one("#settings-sidebar", OptionList).focus()
        self._draw()

    # -- drawing ------------------------------------------------------------

    def _draw(self, highlight: int = 0) -> None:
        """Draw the current menu from live state, with one row highlighted.

        Named `_draw`, not `_render`: `Widget._render` is Textual's own hook for
        the visual a widget paints, and a screen has to leave it alone.
        """
        self.rows = self._row_source()
        menu = self.query_one("#settings-menu", OptionList)
        menu.clear_options()
        menu.add_options(
            [Option(prompt(row.label, row.detail), id=f"row-{index}") for index, row in enumerate(self.rows)]
        )
        if self.rows:
            menu.highlighted = min(max(highlight, 0), len(self.rows) - 1)
        self.query_one("#settings-heading", Static).update(MENUS[self.menu])
        descriptions = {
            "general": "Control editor completion and default query rows. Row changes require Save in the editor.",
            "menus": "Show or hide workspace panes without changing your document.",
            "sources": "Connect a new Cloudflare D1 database or select an existing source. Selecting a source refreshes its schema and completion.",
            "dialects": "Choose the SQL rendering target independently of the active data source. Changing dialect does not execute a query.",
            "ai": "Select any value to edit your OpenAI-compatible provider. Changes are saved only when you choose Save in the editor.",
            "keys": "Keyboard shortcuts from the running app. This reference is read-only.",
        }
        self.query_one("#settings-description", Static).update(
            f"{descriptions[self.menu]}\nSettings: {self.session.settings_path}\n"
            "Settings and connection backups contain plaintext API keys and D1 tokens. Keep them private."
        )
        action = {
            "general": "enter / click toggle or edit",
            "menus": "enter / click toggle",
            "sources": "enter / click select or add",
            "dialects": "enter / click select",
            "ai": "enter / click edit",
            "keys": "read-only reference",
        }[self.menu]
        self.query_one("#settings-hint", Static).update(
            f"↑↓ navigate · {action} · tab switch panel · esc back / close"
        )

    def _row_source(self) -> list[Row]:
        if self.menu == "menus":
            return [
                Row(
                    title,
                    "visible" if self.ide.pane_visible(name) else "hidden",
                    "pane",
                    name,
                )
                for name, title in self.ide.PANE_TITLES.items()
            ]
        if self.menu == "keys":
            return binding_rows()
        if self.menu == "sources":
            return [Row("Add data source", "PostgreSQL, Cloudflare D1 or local SQLite snapshot", "add-source")] + [
                Row(
                    label,
                    "active" if source_id == self.session.active_id else
                    f"{len(self.session.sources[source_id].datasets)} tables" if source_id in self.session.sources else
                    "select to reconnect",
                    "source",
                    source_id,
                )
                for label, source_id in self.session.source_options()
            ]
        if self.menu == "dialects":
            return [Row(spec["label"], self._dialect_detail(spec["name"]), "dialect", spec["name"]) for spec in DIALECTS]
        if self.menu == "ai":
            config = self.session.ai_config
            return [
                Row("Endpoint", config.base_url or "not configured", "ai", "ai-base-url"),
                Row("Model", config.model or "not configured", "ai", "ai-model"),
                Row("API key", "configured" if config.api_key else "not set", "ai", "ai-api-key"),
                Row("AI turns/tool rounds", str(config.max_tool_rounds), "ai", "ai-tool-rounds"),
                Row("Tool calls per round", str(config.max_tool_calls), "ai", "ai-tool-calls"),
                Row("Sample records per read", str(config.sample_rows), "ai", "ai-sample-rows"),
                Row("Maximum attempts", str(config.max_attempts), "ai", "ai-attempts"),
                Row("Request timeout", f"{config.timeout:g} seconds", "ai", "ai-timeout"),
            ]
        return [
            Row("Intellisense", "on" if self.session.intellisense else "off", "intellisense"),
            Row("Default rows returned", str(self.session.default_rows) if self.session.default_rows else
                "0 · no default limit", "default-rows"),
        ]

    def _dialect_detail(self, name: str) -> str:
        """What a dialect row says about itself, honestly."""
        if name == self.session.dialect:
            return "active"
        spec = DIALECT_BY_NAME.get(name)
        if spec is not None and not spec["compiles"]:
            return "known but does not compile in this build"
        return ""

    # -- acting -------------------------------------------------------------

    @on(OptionList.OptionHighlighted, "#settings-sidebar")
    def _category_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        name = event.option.id
        if name in MENUS and name != self.menu:
            self.menu = name
            self._draw()

    @on(OptionList.OptionSelected, "#settings-sidebar")
    def _category_selected(self, event: OptionList.OptionSelected) -> None:
        self.action_details()

    def action_sidebar(self) -> None:
        self.query_one("#settings-sidebar", OptionList).focus()

    def action_details(self) -> None:
        self.query_one("#settings-menu", OptionList).focus()

    @on(OptionList.OptionSelected, "#settings-menu")
    def _selected(self, event: OptionList.OptionSelected) -> None:
        """Carry out the row, then redraw keeping its place in the list."""
        if not 0 <= event.option_index < len(self.rows):
            return
        row = self.rows[event.option_index]
        if not row.action:
            return
        if row.action == "pane":
            self.ide.toggle_pane(row.value)
        elif row.action == "intellisense":
            self.ide.update_settings(intellisense=not self.session.intellisense)
        elif row.action == "default-rows":
            def rows_saved(_) -> None:
                self._draw(event.option_index)
                self.action_details()

            self.app.push_screen(DefaultRowsScreen(self.ide), rows_saved)
            return
        elif row.action == "ai":
            self.app.push_screen(
                AIProviderScreen(self.ide, focus_field=row.value),
                lambda _: self._draw(event.option_index),
            )
            return
        elif row.action == "add-source" or (row.action == "source" and
                                            self.session.saved_source_profile(row.value) is not None):
            if self.ide.refuse_busy("Connect data source"):
                return
            def source_added(source) -> None:
                self.ide._source_added(source)
                self._draw(event.option_index)

            if row.action == "add-source":
                self.app.push_screen(AddSourceModal(self.session), source_added)
            else:
                self.ide.reconnect_source(row.value, source_added)
            return
        elif row.action == "source":
            self.ide.select_source(row.value)
        elif row.action == "dialect":
            self.ide.select_dialect(row.value)
        self._draw(highlight=event.option_index)

    def action_back(self) -> None:
        """Return to categories without losing the page; Escape there closes."""
        if self.query_one("#settings-sidebar", OptionList).has_focus:
            self.dismiss(None)
        else:
            self.action_sidebar()


class DefaultRowsScreen(ModalScreen[None]):
    """Default query limit, persisted only on explicit Save."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    DefaultRowsScreen { align: center middle; background: $background; }
    #rows-settings { width: 76; max-width: 100%; height: auto; max-height: 100%; padding: 1 2; border: round $accent; }
    #rows-settings Static { height: auto; margin-top: 1; }
    #rows-settings-buttons { height: auto; margin-top: 1; }
    #rows-settings-buttons Button { margin-right: 1; min-width: 10; }
    #rows-settings-error { color: $error; }
    """

    def __init__(self, ide: "D8RApp", **kwargs) -> None:
        super().__init__(**kwargs)
        self.ide = ide

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="rows-settings"):
            yield Static("Default rows returned")
            yield Static(
                "Applies to ordinary Run and Compile when the query has no explicit \\limit. "
                "An explicit \\limit always wins, including \\limit 0. "
                "Your document is not edited, and temporary-table writes are not capped. "
                "AI sample reads keep their own limits.", markup=False,
            )
            yield Static("Whole number from 0 to 1,000,000; 0 means no default limit.")
            yield Input(str(self.ide.session.default_rows), type="integer", id="default-rows")
            yield Static("", id="rows-settings-error", markup=False)
            with Horizontal(id="rows-settings-buttons"):
                yield Button("Save", id="rows-settings-save", variant="primary")
                yield Button("Cancel", id="rows-settings-cancel")

    def on_mount(self) -> None:
        self.query_one("#default-rows", Input).focus()

    @on(Button.Pressed, "#rows-settings-save")
    def _save_rows(self) -> None:
        field = self.query_one("#default-rows", Input)
        try:
            value = int(field.value)
            if not 0 <= value <= 1_000_000:
                raise ValueError
        except ValueError:
            self.query_one("#rows-settings-error", Static).update(
                "Enter a whole number from 0 to 1,000,000 (0 means no default limit)."
            )
            field.focus()
            return
        try:
            self.ide.session.update_settings(default_rows=value)
        except ValueError as exc:
            self.query_one("#rows-settings-error", Static).update(str(exc))
            return
        self.dismiss(None)

    @on(Button.Pressed, "#rows-settings-cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class AIProviderScreen(ModalScreen[None]):
    """OpenAI-compatible chat settings, persisted only on explicit Save."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    AIProviderScreen { align: center middle; background: $background; }
    #ai-settings { width: 80; max-width: 100%; height: auto; max-height: 100%; padding: 1 2; border: round $accent; }
    #ai-settings Static { height: auto; margin-top: 1; }
    #ai-settings-buttons { height: auto; margin-top: 1; }
    #ai-settings-buttons Button { margin-right: 1; min-width: 10; }
    #ai-settings-error { color: $error; }
    """

    def __init__(self, ide: "D8RApp", *, focus_field: str = "ai-base-url", **kwargs) -> None:
        super().__init__(**kwargs)
        self.ide = ide
        self.focus_field = focus_field

    def compose(self) -> ComposeResult:
        config = self.ide.session.ai_config
        with VerticalScroll(id="ai-settings"):
            yield Static("AI provider · OpenAI chat completions")
            yield Static(
                f"Save writes provider settings and the API key to {self.ide.session.settings_path}. "
                "Settings and connection backups contain plaintext secrets; keep them private. "
                "Completed chats and drafts are saved locally. Sending a message shares the document and requested schema, "
                "samples and history with this provider. Use HTTPS for remote providers.", markup=False,
            )
            yield Static("Base URL (including /v1), or full /chat/completions URL")
            yield Input(config.base_url, placeholder="https://yolo-auto.com/v1", id="ai-base-url")
            yield Static("Model")
            yield Input(config.model, placeholder="yolo", id="ai-model")
            yield Static("API key (leave blank for an unauthenticated local provider)")
            yield Input(config.api_key, password=True, id="ai-api-key")
            yield Static("AI turns/tool rounds (1–50)")
            yield Input(str(config.max_tool_rounds), type="integer", id="ai-tool-rounds")
            yield Static("Rounds count model context batches, not chat messages. After the round budget, "
                         "one final answer is requested with tools disabled.", markup=False)
            yield Static("Tool calls per round (1–16)")
            yield Input(str(config.max_tool_calls), type="integer", id="ai-tool-calls")
            yield Static("Schema, sample, history and validation reads count as tool calls; "
                         "several calls may share a round.", markup=False)
            yield Static("Sample records per read (1–100)")
            yield Input(str(config.sample_rows), type="integer", id="ai-sample-rows")
            yield Static("Maximum attempts (1–5; transient failures only)")
            yield Input(str(config.max_attempts), type="integer", id="ai-attempts")
            yield Static("1 initial attempt + remaining attempts as shared retries per message, "
                         "independent of the round budget.", markup=False)
            yield Static("Request timeout in seconds (including streaming)")
            yield Input(str(config.timeout), type="number", id="ai-timeout")
            yield Static("", id="ai-settings-error", markup=False)
            with Horizontal(id="ai-settings-buttons"):
                yield Button("Save", id="ai-settings-save", variant="primary")
                yield Button("Import yolo key", id="ai-settings-import")
                yield Button("Cancel", id="ai-settings-cancel")

    def on_mount(self) -> None:
        self.query_one(f"#{self.focus_field}", Input).focus()

    @on(Button.Pressed, "#ai-settings-save")
    def _save_ai(self) -> None:
        try:
            config = AIConfig(
                base_url=self.query_one("#ai-base-url", Input).value.strip(),
                model=self.query_one("#ai-model", Input).value.strip(),
                api_key=self.query_one("#ai-api-key", Input).value.strip(),
                max_attempts=int(self.query_one("#ai-attempts", Input).value),
                timeout=float(self.query_one("#ai-timeout", Input).value),
                max_tool_rounds=int(self.query_one("#ai-tool-rounds", Input).value),
                max_tool_calls=int(self.query_one("#ai-tool-calls", Input).value),
                sample_rows=int(self.query_one("#ai-sample-rows", Input).value),
            )
        except ValueError:
            self.query_one("#ai-settings-error", Static).update(
                "Enter whole numbers for attempts, tool rounds, tool calls and sample records, "
                "and a number for the timeout."
            )
            return
        try:
            config.validate()
            self.ide.session.update_settings(ai=config)
        except ValueError as exc:
            self.query_one("#ai-settings-error", Static).update(str(exc))
            return
        self.dismiss(None)

    @on(Button.Pressed, "#ai-settings-import")
    def _import_ai(self) -> None:
        try:
            config = import_yolo_config()
        except ValueError as exc:
            self.query_one("#ai-settings-error", Static).update(str(exc))
            return
        self.query_one("#ai-base-url", Input).value = config.base_url
        self.query_one("#ai-model", Input).value = config.model
        self.query_one("#ai-api-key", Input).value = config.api_key
        self.query_one("#ai-settings-error", Static).update("Imported into this form only; Save persists these settings and the API key.")

    @on(Button.Pressed, "#ai-settings-cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)
