"""The `\\settings` menu: every setting anyQL has, on one full-screen page.

The palette's `Settings` row (type `\\settings`) opens it, and it covers the
whole screen because nothing behind it is needed while it is open. It is a
two-level menu: the root lists the submenus, selecting one swaps the list, and
`escape` goes back a level — out of Settings entirely from the root.

Rows are read from live state every time a menu is drawn, so a marker always
says what is true and a change lands in the IDE at once. Nothing here persists.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from anyql.engine import DIALECT_BY_NAME, DIALECTS

from .palette import prompt

if TYPE_CHECKING:
    from .app import AnyqlApp

# The menus this screen can show, root first.
MENUS: dict[str, str] = {
    "root": "every setting",
    "menus": "Show/Hide Menus",
    "keys": "Keybindings",
    "sources": "Data source",
    "dialects": "Dialect",
}

HINT = "enter change · escape back · escape again leaves Settings"


@dataclass(frozen=True)
class Row:
    """One menu row: what it reads as, and what selecting it does."""

    label: str
    detail: str = ""
    # menu | pane | intellisense | source | dialect; empty for a plain reference row.
    action: str = ""
    value: str = ""


def binding_rows() -> list[Row]:
    """Every key the IDE answers to, read from the classes that declare them.

    Nothing is retyped here: each row is the `Binding` the widget or the app
    actually carries, so the reference cannot drift from the keymap.
    """
    from textual.app import App as TextualApp

    from .add_source import AddSourceModal
    from .app import AnyqlApp, EditorPane, ResultsTable

    groups = (
        ("app", AnyqlApp.BINDINGS),
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

    BINDINGS = [Binding("escape", "back", "Back")]

    def __init__(self, ide: "AnyqlApp", **kwargs) -> None:
        super().__init__(**kwargs)
        self.ide = ide
        self.menu = "root"
        self.rows: list[Row] = []

    @property
    def session(self):
        """The session whose state the menu reads and writes."""
        return self.ide.session

    def compose(self) -> ComposeResult:
        with Vertical(id="settings"):
            yield Static("Settings", id="settings-title")
            yield Static("", id="settings-crumb")
            yield OptionList(id="settings-menu")
            yield Static(HINT, id="settings-hint")

    def on_mount(self) -> None:
        self.query_one("#settings-menu", OptionList).focus()
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
        crumb = MENUS[self.menu] if self.menu == "root" else f"Settings › {MENUS[self.menu]}"
        self.query_one("#settings-crumb", Static).update(crumb)

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
            return [
                Row(
                    source.display,
                    "active" if source_id == self.session.active_id else f"{len(source.datasets)} tables",
                    "source",
                    source_id,
                )
                for source_id, source in self.session.sources.items()
            ]
        if self.menu == "dialects":
            return [Row(spec["label"], self._dialect_detail(spec["name"]), "dialect", spec["name"]) for spec in DIALECTS]
        return [
            Row("Show/Hide Menus", "results · sql · schema", "menu", "menus"),
            Row("Intellisense", "on" if self.session.intellisense else "off", "intellisense"),
            Row("Keybindings", f"{len(binding_rows())} keys", "menu", "keys"),
            Row(
                "Data source",
                f"{self.session.active_id} · {len(self.session.sources)} registered",
                "menu",
                "sources",
            ),
            Row(
                "Dialect",
                f"{self.session.dialect} · {len(DIALECTS)} targets",
                "menu",
                "dialects",
            ),
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

    @on(OptionList.OptionSelected, "#settings-menu")
    def _selected(self, event: OptionList.OptionSelected) -> None:
        """Carry out the row, then redraw keeping its place in the list."""
        if not 0 <= event.option_index < len(self.rows):
            return
        row = self.rows[event.option_index]
        if not row.action:
            return
        if row.action == "menu":
            self.menu = row.value
            self._draw()
            return
        if row.action == "pane":
            self.ide.toggle_pane(row.value)
        elif row.action == "intellisense":
            self.session.intellisense = not self.session.intellisense
        elif row.action == "source":
            self.ide.select_source(row.value)
        elif row.action == "dialect":
            self.ide.select_dialect(row.value)
        self._draw(highlight=event.option_index)

    def action_back(self) -> None:
        """Escape: back to the root menu — and out of Settings from there."""
        if self.menu == "root":
            self.dismiss(None)
            return
        self.menu = "root"
        self._draw()
