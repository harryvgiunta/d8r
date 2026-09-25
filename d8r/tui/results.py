"""Buffered result selection and explicit CSV export; NULL is an empty CSV/TSV cell."""

from __future__ import annotations

import asyncio
import csv
import io
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from rich.style import Style
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Text
from textual.geometry import Size
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Select, Static

from .session import PREVIEW_ROW_CAP, RunOutcome

MIN_COLUMN_WIDTH = 4


@dataclass(frozen=True)
class ResultSnapshot:
    columns: tuple[str, ...]
    rows: tuple[tuple[object, ...], ...]
    total: int
    selected: tuple[int, ...]


class ResultsTable(DataTable):
    """A cell-cursor grid with independent row selection and column resizing."""

    COMPONENT_CLASSES = DataTable.COMPONENT_CLASSES | {"results--selected-row"}
    DEFAULT_CSS = """
    ResultsTable {
        border: solid $border;
        border-subtitle-align: left;
        & > .results--selected-row {
            background: $primary-darken-2;
            color: $text;
            text-style: bold;
        }
    }
    """
    BINDINGS = [
        Binding("shift+left", "narrow_column", "Narrow column", show=False),
        Binding("shift+right", "widen_column", "Widen column", show=False),
        Binding("shift+up", "extend_rows(-1)", "Extend rows up", show=False),
        Binding("shift+down", "extend_rows(1)", "Extend rows down", show=False),
        Binding("space", "toggle_row", "Toggle row", show=False),
        Binding("ctrl+a", "select_all_rows", "Select buffered rows", show=False),
        Binding("escape", "clear_rows", "Clear row selection", show=False),
        Binding("ctrl+c,super+c", "copy_rows", "Copy rows", show=False),
    ]

    def __init__(self, *args, **kwargs) -> None:
        self.raw_columns: tuple[str, ...] = ()
        self.raw_rows: tuple[tuple[object, ...], ...] = ()
        self.result_total = 0
        self.selected_rows: set[int] = set()
        self._range_anchor: int | None = None
        self._range_end: int | None = None
        super().__init__(*args, **kwargs)
        self.border_title = "Shift+↑/↓ range · Space toggle · Ctrl+C copy"
        self._selection_caption()

    def show_result(self, outcome: RunOutcome) -> None:
        """Replace the grid and its raw buffer, resetting selection and cursor."""
        self.clear(columns=True)
        self.raw_columns = tuple(outcome.columns) if not outcome.error else ()
        self.raw_rows = tuple(tuple(row) for row in outcome.rows) if self.raw_columns else ()
        self.result_total = outcome.total if self.raw_columns else 0
        self._range_anchor = self._range_end = None
        self._set_selection(set())
        if not self.raw_columns:
            return
        self.add_columns(*[
            Text.assemble(name, (" · " + outcome.dtypes[index], "dim"))
            if index < len(outcome.dtypes) and outcome.dtypes[index] else Text(name)
            for index, name in enumerate(self.raw_columns)
        ])
        self.add_rows([
            [Text("NULL", style="dim") if value is None else Text(str(value)) for value in row]
            for row in self.raw_rows
        ])
        self.move_cursor(row=0, column=0)

    def _selection_caption(self) -> None:
        self.border_subtitle = (
            f"{len(self.selected_rows):,} selected · {len(self.raw_rows):,} buffered / "
            f"{self.result_total:,} total · preview cap {PREVIEW_ROW_CAP:,}"
        )

    def _set_selection(self, rows: set[int]) -> None:
        self.selected_rows = rows
        # Textual 8's row and line caches do not include owner selection state.
        self._clear_caches()
        self._selection_caption()
        self.refresh()

    def _get_row_style(self, row_index: int, base_style: Style) -> Style:
        # DataTable uses this hook for every cell and the trailing row fill.
        style = super()._get_row_style(row_index, base_style)
        if row_index in self.selected_rows:
            style += self.get_component_rich_style("results--selected-row")
        return style

    def action_cursor_up(self) -> None:
        self._range_anchor = self._range_end = None
        super().action_cursor_up()

    def action_cursor_down(self) -> None:
        self._range_anchor = self._range_end = None
        super().action_cursor_down()

    def action_extend_rows(self, delta: int) -> None:
        if not self.raw_rows:
            return
        current = self.cursor_row
        if self._range_anchor is None or current != self._range_end:
            self._range_anchor = current
        target = max(0, min(len(self.raw_rows) - 1, current + delta))
        self.move_cursor(row=target)
        self._range_end = target
        first, last = sorted((self._range_anchor, target))
        self._set_selection(set(range(first, last + 1)))

    def action_toggle_row(self) -> None:
        if not self.raw_rows:
            return
        self._range_anchor = self._range_end = None
        self._set_selection(self.selected_rows ^ {self.cursor_row})

    def action_select_all_rows(self) -> None:
        self._range_anchor = self._range_end = None
        self._set_selection(set(range(len(self.raw_rows))))

    def action_clear_rows(self) -> None:
        self._range_anchor = self._range_end = None
        self._set_selection(set())

    def action_copy_rows(self) -> None:
        if not self.raw_columns or not self.raw_rows:
            self.app.notify("No buffered result rows to copy.", severity="warning")
            return
        indices = sorted(self.selected_rows) if self.selected_rows else [self.cursor_row]
        output = io.StringIO(newline="")
        writer = csv.writer(output, delimiter="\t")
        writer.writerow(self.raw_columns)
        writer.writerows(self.raw_rows[index] for index in indices)
        self.app.copy_to_clipboard(output.getvalue())
        self.app.notify(f"Copied {len(indices):,} buffered row(s) with headers as TSV. NULL is an empty cell.")

    def action_export(self) -> None:
        if not self.raw_columns:
            self.app.notify("No result columns to export. Run a query first.", severity="warning")
            return
        # Isolate even nested engine values from future result mutations.
        snapshot = ResultSnapshot(
            self.raw_columns, deepcopy(self.raw_rows), self.result_total,
            tuple(sorted(self.selected_rows)),
        )
        self.app.push_screen(ExportModal(snapshot))

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
            column.width = before - 2 * self.cell_padding
            column.auto_width = False
        column.width = max(MIN_COLUMN_WIDTH, column.width + delta)
        after = column.get_render_width(self)
        width, height = self.virtual_size
        self.virtual_size = Size(width + after - before, height)
        self._clear_caches()
        self.refresh_column(index)


class ExportModal(ModalScreen):
    """Export a captured buffer only; creating or replacing files is explicit."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    ExportModal {
        align: center middle;
        background: $background 65%;
        #export-dialog {
            width: 78;
            max-width: 95%;
            height: 26;
            max-height: 95%;
            border: thick $primary;
            background: $surface;
            padding: 1 2;
        }
        #export-title { text-style: bold; margin-bottom: 1; }
        #export-form { height: 1fr; }
        #export-summary, #export-message { height: auto; margin: 1 0; }
        #export-buttons { height: auto; }
        Button { margin-right: 1; }
        #export-overwrite { display: none; }
    }
    """

    def __init__(self, snapshot: ResultSnapshot, **kwargs) -> None:
        super().__init__(**kwargs)
        self.snapshot = snapshot
        self._overwrite_path: Path | None = None
        self._busy = False

    def compose(self) -> ComposeResult:
        with Vertical(id="export-dialog"):
            yield Static("Export buffered results to CSV", id="export-title")
            with VerticalScroll(id="export-form"):
                yield Static("Destination path (UTF-8 CSV; NULL becomes an empty cell)", markup=False)
                yield Input(placeholder="Enter a destination .csv path", id="export-path")
                options = [("All buffered rows", "all")]
                if self.snapshot.selected:
                    options.append(("Selected rows", "selected"))
                yield Select(options, value="selected" if self.snapshot.selected else "all",
                             allow_blank=False, id="export-scope")
                yield Static("", id="export-summary", markup=False)
                yield Static("", id="export-message", markup=False)
            with Horizontal(id="export-buttons"):
                yield Button("Export CSV", variant="primary", id="export-save")
                yield Button("Overwrite file", variant="error", id="export-overwrite")
                yield Button("Cancel", id="export-cancel")

    def on_mount(self) -> None:
        self._update_summary()
        self.query_one("#export-path", Input).focus()

    def _indices(self) -> tuple[int, ...] | range:
        if self.query_one("#export-scope", Select).value == "selected":
            return self.snapshot.selected
        return range(len(self.snapshot.rows))

    def _update_summary(self) -> None:
        snapshot = self.snapshot
        scope = self.query_one("#export-scope", Select).value
        self.query_one("#export-summary", Static).update(
            f"Exporting {len(self._indices()):,} {scope} row(s), plus headers.\n"
            f"{len(snapshot.rows):,} buffered of {snapshot.total:,} total rows. "
            f"Preview cap: {PREVIEW_ROW_CAP:,} rows.\n"
            "Only this captured buffer is exported; the query is not rerun."
        )

    @on(Select.Changed, "#export-scope")
    def _scope_changed(self) -> None:
        self._reset_confirmation()
        self._update_summary()

    @on(Input.Changed, "#export-path")
    def _path_changed(self) -> None:
        self._reset_confirmation()

    def _reset_confirmation(self) -> None:
        self._overwrite_path = None
        self.query_one("#export-overwrite", Button).display = False
        self.query_one("#export-message", Static).update("")

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for selector in ("#export-path", "#export-scope", "#export-save",
                         "#export-overwrite", "#export-cancel"):
            self.query_one(selector).disabled = busy

    @on(Button.Pressed, "#export-save")
    def _export_pressed(self) -> None:
        self._begin_export(overwrite=False)

    @on(Button.Pressed, "#export-overwrite")
    def _overwrite_pressed(self) -> None:
        self._begin_export(overwrite=True)

    def _begin_export(self, *, overwrite: bool) -> None:
        if self._busy:
            return
        destination = self.query_one("#export-path", Input).value.strip()
        if not destination:
            self.query_one("#export-message", Static).update("Enter an explicit destination path.")
            return
        try:
            path = Path(destination).expanduser().absolute()
        except (OSError, ValueError, RuntimeError) as exc:
            self._show_error(exc)
            return
        if overwrite and path != self._overwrite_path:
            return
        indices = self._indices()
        self._set_busy(True)
        self.query_one("#export-message", Static).update("Writing CSV…")
        self.run_worker(self._export(path, indices, overwrite), name="Export CSV", exit_on_error=False)

    async def _export(self, path: Path, indices: tuple[int, ...] | range, overwrite: bool) -> None:
        try:
            await asyncio.to_thread(self._write_csv, path, indices, overwrite)
        except FileExistsError:
            self._overwrite_path = path
            self.query_one("#export-overwrite", Button).display = True
            self.query_one("#export-message", Static).update(
                f"File already exists: {path}\nNothing was written. Choose Overwrite file to replace it."
            )
        except (OSError, ValueError, csv.Error) as exc:
            self._show_error(exc)
        else:
            self.app.notify(f"Exported {len(indices):,} buffered row(s) to {path}")
            self.dismiss(path)
        finally:
            self._set_busy(False)

    def _write_csv(self, path: Path, indices: tuple[int, ...] | range, overwrite: bool) -> None:
        # Exclusive creation also guards files appearing after the form opened.
        with path.open("w" if overwrite else "x", encoding="utf-8", newline="") as output:
            writer = csv.writer(output)
            writer.writerow(self.snapshot.columns)
            writer.writerows(self.snapshot.rows[index] for index in indices)

    def _show_error(self, exc: Exception) -> None:
        message = f"CSV export failed: {exc}"
        self.query_one("#export-message", Static).update(message)
        self.app.notify(message, severity="error")

    @on(Button.Pressed, "#export-cancel")
    def action_cancel(self) -> None:
        # A committed write cannot be cancelled halfway by closing its screen.
        if not self._busy:
            self.dismiss(None)
