"""The `\\fn` page: the function library, and the editor for one function.

A table-valued function here is a named anyQL document with a positional
signature: the body is an ordinary query whose `@params` are filled with the
arguments of each call (the engine sees literals; ibis never learns a function
exists). This screen is where those definitions are authored — a list of what
the session holds, and, on the right, the fields for the selected (or new)
function: name, description, parameter signature, body, and the arguments for a
trial call. `Run preview` saves, then calls it through the same path a
document's `\\from name(args)` takes, so the grid shows what the editor gets.

Definitions are saved locally through `Session` and restored on the next launch.
Draft edits and AI Apply are not saved until Save or Run preview is used.
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Text
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, OptionList, Select, Static, TextArea
from textual.widgets.option_list import Option

from anyql.ai.context import AIProposal
from anyql.query import is_identifier

from .ai import AIPanel, AITarget

from .palette import CommandPalette, EditorPane, prompt

if TYPE_CHECKING:
    from .app import AnyqlApp

NEW = "＋ New function…"

HINT = "select a function · ctrl+r preview · ctrl+d delete · escape intellisense · ctrl+c back"

PARAMETER_GUIDE = r"""1. Declare names, not values
Parameters: min_amount
No @, types or defaults here.
Names must be unique identifiers.

2. Use @name in the body
Keep placeholders unquoted:
\from events
\where amount > @min_amount
\select user_id, amount

3. Try it (Source target: demo)
Name: events_above
Preview arguments: 10
Run preview saves, then runs it.

4. Call it from a document
\from events_above(10)
\select *

Multiple parameters use order:
min_amount, event_kind
Arguments: 10, "purchase"
10 binds @min_amount;
"purchase" binds @event_kind.

Quote text values, not numbers.
Supply one value per parameter;
use values only, not name=value.
No parameters? Leave both fields
blank and call name()."""


class FnBodyPane(EditorPane):
    """Keep form navigation when no completion is being accepted."""

    def action_palette_tab(self) -> None:
        if self.palette.is_open:
            self.palette.accept_highlighted()
        else:
            self.screen.focus_next()


class FnScreen(ModalScreen[None]):
    """The function library: `\\fn` opens it; every save lands at once."""

    BINDINGS = [
        Binding("ctrl+c", "back", "Back", priority=True),
        Binding("ctrl+r", "preview", "Preview"),
        Binding("ctrl+d", "delete", "Delete"),
    ]

    def __init__(
        self, ide: "AnyqlApp", focus: str = "", new_name: str = "", new_body: str = "", **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.ide = ide
        # Initial prefills belong only to the first new draft, never saved entries.
        self.selected = focus if focus in ide.session.fns else ""
        self._prefill = new_name if not focus else ""
        self._prefill_body = new_body if not focus else ""
        self.source_id = ide.session.active_id
        self._draft_identity = 0

    @property
    def session(self):
        return self.ide.session

    def compose(self) -> ComposeResult:
        with Horizontal(id="fn"):
            with Vertical(id="fn-list-pane"):
                yield Static("Functions", id="fn-title")
                yield OptionList(id="fn-list")
                with VerticalScroll(id="fn-param-guide"):
                    yield Static("How parameters work", classes="guide-title")
                    yield Static(PARAMETER_GUIDE, markup=False)
                yield Static(HINT, id="fn-hint")
                yield Static(
                    f"Save and Run preview persist locally to {self.session.storage_path}. "
                    "Draft edits and AI Apply are not auto-saved.",
                    markup=False,
                )
            with Vertical(id="fn-content"):
                yield Static("Source — the data this function uses", classes="field-label")
                yield Select(
                    [(source.display, source.id) for source in self.session.sources.values()],
                    value=self.source_id, allow_blank=False, id="fn-source",
                )
                with VerticalScroll(id="fn-editor"):
                    yield Static("", id="fn-status", markup=False)
                    yield Button("Make with AI", id="fn-ai", variant="primary")
                    yield Static("Describe what you need, or edit the fields below.", markup=False)
                    yield Static("Name", classes="field-label")
                    yield Input(placeholder="monthly", id="fn-name")
                    yield Static("Description", classes="field-label")
                    yield Input(placeholder="one line on what it returns", id="fn-doc")
                    yield Static("Parameters — names in argument order (no @)", classes="field-label")
                    yield Input(placeholder="min_amount", id="fn-params")
                    yield Static(
                        "Body — an anyQL document; `@param` binds an argument",
                        classes="field-label",
                    )
                    with FnBodyPane(id="fn-body-pane"):
                        yield TextArea(id="fn-body", show_line_numbers=True)
                        yield CommandPalette(
                            self.session, source_id=self.source_id, workspace_actions=False,
                            parameters=self._parameter_names, id="fn-palette", markup=False,
                        )
                    yield Static("Preview arguments — values in the same order; quote text", classes="field-label")
                    yield Input(placeholder='10 (or 10, "purchase" for two parameters)', id="fn-args")
                    yield DataTable(id="fn-grid")
                with Horizontal(id="fn-buttons"):
                    yield Button("Save", id="fn-save", variant="primary")
                    yield Button("Run preview", id="fn-preview")
                    yield Button("Delete", id="fn-delete", variant="error")
                yield AIPanel(
                    self.session, self._ai_target, self._apply_ai_draft,
                    self._return_from_ai, function_mode=True, id="fn-ai-panel",
                )

    def on_mount(self) -> None:
        self.query_one("#fn-grid", DataTable).cursor_type = "cell"
        focus_name = bool(self._prefill_body)
        self._select(self.selected)
        self._prefill = ""
        self._prefill_body = ""
        if focus_name:
            self.query_one("#fn-name", Input).focus()

    @on(Select.Changed, "#fn-source")
    def _source_changed(self, event: Select.Changed) -> None:
        if not isinstance(event.value, str) or event.value == self.source_id:
            return
        self.source_id = event.value
        self.ai_panel.target_changed()
        palette = self.query_one(CommandPalette)
        palette.source_id = self.source_id
        palette.close()
        palette.sync()
        self.query_one("#fn-grid", DataTable).clear(columns=True)
        self._status(f"target · {self.session.sources[self.source_id].display}")

    def _parameter_names(self) -> list[str]:
        """Read this form's unsaved signature; invalid/duplicate names aren't offers."""
        names = self.query_one("#fn-params", Input).value.split(",")
        return list(dict.fromkeys(name for part in names if is_identifier(name := part.strip())))

    @on(Input.Changed, "#fn-params")
    def _parameters_changed(self) -> None:
        self.query_one(CommandPalette).sync()


    @property
    def ai_panel(self) -> AIPanel:
        return self.query_one("#fn-ai-panel", AIPanel)

    def _ai_target(self) -> AITarget:
        name, params, body, doc = self._fields()
        return AITarget(
            (self.source_id, self._draft_identity), self.source_id, body,
            tuple(part.strip() for part in params.split(",") if part.strip()),
            json.dumps({"name": name, "description": doc, "parameters": params,
                        "arguments": self.query_one("#fn-args", Input).value}, ensure_ascii=False),
            function_name=self.selected,
        )

    def _apply_ai_draft(self, proposal: AIProposal) -> None:
        fn = proposal.function
        assert fn is not None
        self.query_one("#fn-name", Input).value = fn.name.strip()
        self.query_one("#fn-doc", Input).value = fn.doc.strip()
        self.query_one("#fn-params", Input).value = ", ".join(fn.params)
        self.query_one("#fn-args", Input).value = proposal.arguments
        self.query_one("#fn-body", TextArea).load_text(proposal.body)
        self.query_one(CommandPalette).close()
        self.query_one("#fn-grid", DataTable).clear(columns=True)
        self._status("AI draft applied · review, then Save or Run preview")

    def _return_from_ai(self) -> None:
        self.query_one("#fn").remove_class("ai-open")
        self.query_one("#fn-name", Input).focus()

    @on(Input.Changed, "#fn-name")
    @on(Input.Changed, "#fn-doc")
    @on(Input.Changed, "#fn-params")
    @on(Input.Changed, "#fn-args")
    @on(TextArea.Changed, "#fn-body")
    def _ai_draft_changed(self) -> None:
        self.ai_panel.target_changed()

    @on(Button.Pressed, "#fn-ai")
    def _make_with_ai(self, event: Button.Pressed) -> None:
        event.stop()
        self.query_one(CommandPalette).close()
        self.query_one("#fn").add_class("ai-open")
        self.ai_panel.open()

    # -- drawing -------------------------------------------------------------

    def _draw_list(self, highlight: int = 0) -> None:
        """Redraw the library list from live state, with one row highlighted."""
        rows: list[tuple[str, str]] = []
        for fn in self.session.fns.values():
            signature = ", ".join(fn.params) if fn.params else "no arguments"
            detail = f"{signature} · {fn.doc}" if fn.doc else f"{signature} · {len(fn.body.splitlines())} lines"
            rows.append((fn.name, detail))
        rows.append((NEW, "define a new function"))
        listing = self.query_one("#fn-list", OptionList)
        listing.clear_options()
        listing.add_options([Option(prompt(name, detail), id=name) for name, detail in rows])
        listing.highlighted = max(0, min(highlight, len(rows) - 1))

    def _select(self, name: str) -> None:
        """Highlight `name` in the list and load it (or a blank form) into the fields."""
        keys = [*self.session.fns]
        index = keys.index(name) if name in keys else len(keys)
        self.selected = keys[index] if index < len(keys) else ""
        self._draw_list(highlight=index)
        self._load(self.selected)

    def _load(self, name: str) -> None:
        """Put one function's text in the fields; a new function shows blanks."""
        self._draft_identity += 1
        self.ai_panel.target_changed()
        self.query_one(CommandPalette).close()
        fn = self.session.fns.get(name)
        self.query_one("#fn-delete", Button).disabled = fn is None
        if fn is None:
            self.query_one("#fn-name", Input).value = self._prefill
            self.query_one("#fn-doc", Input).value = ""
            self.query_one("#fn-params", Input).value = ""
            self.query_one("#fn-body", TextArea).text = self._prefill_body
            self._status("new function")
            return
        self.query_one("#fn-name", Input).value = fn.name
        self.query_one("#fn-doc", Input).value = fn.doc
        self.query_one("#fn-params", Input).value = ", ".join(fn.params)
        self.query_one("#fn-body", TextArea).text = fn.body
        self._status(f"{fn.name} · saved locally")

    def _status(self, text: str, error: bool = False) -> None:
        widget = self.query_one("#fn-status", Static)
        widget.update(text)
        widget.set_class(error, "error")

    # -- acting --------------------------------------------------------------

    @on(OptionList.OptionSelected, "#fn-list")
    def _picked(self, event: OptionList.OptionSelected) -> None:
        name = str(event.option_id or "")
        if name == NEW:
            self._select("")
            self.query_one("#fn-name", Input).focus()
            return
        self._select(name)

    def _fields(self) -> tuple[str, str, str, str]:
        return (
            self.query_one("#fn-name", Input).value,
            self.query_one("#fn-params", Input).value,
            self.query_one("#fn-body", TextArea).text,
            self.query_one("#fn-doc", Input).value,
        )

    def _save(self) -> str | None:
        """Save the fields; return the error text, or None on success."""
        if self.ide.refuse_busy("Save function"):
            return self.session.busy
        name, params_text, body, doc = self._fields()
        try:
            with self.session.target_source(self.source_id):
                fn = self.session.save_fn(name, params_text, body, doc)
        except ValueError as exc:
            self._status(str(exc), error=True)
            return str(exc)
        self._select(fn.name)
        self._status(f'function "{fn.name}" saved locally · {len(self.session.fns)} in library')
        return None

    @on(Button.Pressed, "#fn-save")
    def _save_pressed(self, event: Button.Pressed) -> None:
        self._save()

    @on(Button.Pressed, "#fn-preview")
    def _preview_pressed(self, event: Button.Pressed) -> None:
        self.action_preview()

    @on(Button.Pressed, "#fn-delete")
    def _delete_pressed(self, event: Button.Pressed) -> None:
        self.action_delete()

    def action_preview(self) -> None:
        """Save, then run a call with the arguments field; rows render in the grid."""
        if self.ai_panel.has_focus_within:
            return
        if self._save() is not None:
            return
        name = self.query_one("#fn-name", Input).value.strip()
        args_text = self.query_one("#fn-args", Input).value
        with self.session.target_source(self.source_id):
            outcome = self.session.fn_preview(name, args_text)
        grid = self.query_one("#fn-grid", DataTable)
        grid.clear(columns=True)
        if outcome.error:
            self._status(f"preview failed · {outcome.error}", error=True)
            return
        grid.add_columns(
            *[
                Text.assemble(column, (" · " + dtype, "dim")) if dtype else Text(column)
                for column, dtype in zip(outcome.columns, outcome.dtypes)
            ]
        )
        if outcome.rows:
            grid.add_rows([[_cell(value) for value in row] for row in outcome.rows])
            grid.move_cursor(row=0, column=0)
        self._status(f"preview · {outcome.status}")

    def action_delete(self) -> None:
        if self.ai_panel.has_focus_within:
            return
        if self.ide.refuse_busy("Delete function"):
            return
        name = self.query_one("#fn-name", Input).value.strip()
        if name not in self.session.fns:
            self._status("nothing to delete — this function is not saved", error=True)
            return
        try:
            self.session.delete_fn(name)
        except ValueError as exc:
            self._status(str(exc), error=True)
            return
        self._select("")
        self._status(f'function "{name}" deleted from local memory')

    def action_back(self) -> None:
        """Ctrl+C leaves the library; the schema seam already reflects the saves."""
        self.ai_panel.action_cancel()
        self.dismiss(None)


def _cell(value: object) -> str:
    """A cell as the preview grid shows it: NULL for nothing, compact otherwise."""
    return "NULL" if value is None else str(value)
