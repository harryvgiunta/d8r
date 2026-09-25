"""Real result widgets, clipboard payloads and explicitly requested CSV files."""

from __future__ import annotations

import asyncio
import csv
import io
from datetime import date
from decimal import Decimal

from textual.app import App, ComposeResult
from textual.widgets import Button, Input, Select, Static

from d8r.tui.results import ExportModal, ResultsTable
from d8r.tui.session import PREVIEW_ROW_CAP, RunOutcome


def outcome() -> RunOutcome:
    return RunOutcome(
        ok=True,
        columns=['[label]\t"name"', "amount", "day"],
        dtypes=["string", "decimal", "date"],
        rows=[
            ['first\t"quoted"\nline', Decimal("1.20"), date(2026, 9, 24)],
            ["café,second", None, date(2026, 9, 25)],
            ["third", Decimal("3.40"), None],
            ["fourth", Decimal("4.50"), None],
        ],
        total=PREVIEW_ROW_CAP + 3,
    )


class ResultsApp(App):
    CSS = "ResultsTable { height: 1fr; }"

    def __init__(self, result: RunOutcome) -> None:
        super().__init__()
        self.result = result

    def compose(self) -> ComposeResult:
        yield ResultsTable(id="grid", zebra_stripes=True, cursor_type="cell")

    def on_mount(self) -> None:
        table = self.query_one(ResultsTable)
        table.show_result(self.result)
        table.focus()


def run_grid(scenario, result: RunOutcome | None = None) -> None:
    app = ResultsApp(result if result is not None else outcome())

    async def drive():
        async with app.run_test(size=(110, 35)) as pilot:
            await pilot.pause()
            await scenario(app, pilot, app.query_one(ResultsTable))

    asyncio.run(drive())


def serialized_rows(result: RunOutcome, indices) -> list[list[str]]:
    return [result.columns] + [
        ["" if value is None else str(value) for value in result.rows[index]]
        for index in indices
    ]


def read_csv(path) -> list[list[str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.reader(stream))


async def save(pilot, app, button="#export-save") -> None:
    # Textual ignores clicks during its previous button-press effect. Waiting
    # for workers/idle does not wait for this timer, so rapid retries must do so.
    target = app.screen.query_one(button, Button)
    if target.has_class("-active"):
        await pilot.pause(target.active_effect_duration)
    await pilot.click(button)
    await app.workers.wait_for_complete()
    await pilot.pause()


def test_range_and_disjoint_selection_copy_raw_tsv_and_highlight_full_rows():
    result = outcome()

    async def scenario(app, pilot, table):
        await pilot.press("shift+down", "shift+down")
        assert table.selected_rows == {0, 1, 2}
        await pilot.press("shift+up")
        assert table.selected_rows == {0, 1}
        await pilot.press("down", "down", "right", "space")
        assert (table.cursor_row, table.cursor_column) == (3, 1)
        assert table.selected_rows == {0, 1, 3}
        assert "3 selected" in str(table.border_subtitle)

        # Inspect the rendered surface, not just the selected set: the noncursor
        # row's text and trailing blank space both differ from an unselected row.
        selected_line = list(table.render_line(1))
        plain_line = list(table.render_line(3))
        assert selected_line[0].style.bgcolor != plain_line[0].style.bgcolor
        assert selected_line[-1].style.bgcolor != plain_line[-1].style.bgcolor
        for segment in selected_line:
            if "1.20" in segment.text or "2026-09-24" in segment.text:
                assert segment.style.bold

        await pilot.press("ctrl+c")
        assert list(csv.reader(io.StringIO(app.clipboard), delimiter="\t")) == serialized_rows(result, [0, 1, 3])
        assert "decimal" not in app.clipboard

        await pilot.press("escape", "ctrl+c")
        assert not table.selected_rows
        assert list(csv.reader(io.StringIO(app.clipboard), delimiter="\t")) == serialized_rows(result, [3])

    run_grid(scenario, result)


def test_select_all_resets_on_new_result_and_resize_still_works():
    async def scenario(app, pilot, table):
        await pilot.press("ctrl+a")
        assert table.selected_rows == {0, 1, 2, 3}
        width = table.ordered_columns[0].get_render_width(table)
        await pilot.press("shift+right")
        assert table.ordered_columns[0].get_render_width(table) == width + 2
        await pilot.press("shift+left")
        assert table.ordered_columns[0].get_render_width(table) == width
        table.show_result(RunOutcome(ok=True, columns=["fresh"], rows=[[7]], total=1))
        await pilot.pause()
        assert not table.selected_rows
        assert table.row_count == 1
        await pilot.press("ctrl+c")
        assert list(csv.reader(io.StringIO(app.clipboard), delimiter="\t")) == [["fresh"], ["7"]]

    run_grid(scenario)


def test_export_selected_and_all_scopes_use_raw_values(tmp_path):
    result = outcome()

    async def scenario(app, pilot, table):
        await pilot.press("down", "space", "down", "down", "space")
        table.action_export()
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, ExportModal)
        summary = str(modal.query_one("#export-summary", Static).content)
        assert "2 selected row(s)" in summary
        assert f"4 buffered of {result.total:,} total" in summary
        assert f"Preview cap: {PREVIEW_ROW_CAP:,}" in summary
        selected_path = tmp_path / "selected.csv"
        modal.query_one("#export-path", Input).value = str(selected_path)
        await pilot.pause()
        assert not selected_path.exists()
        await save(pilot, app)
        assert read_csv(selected_path) == serialized_rows(result, [1, 3])

        table.action_export()
        await pilot.pause()
        modal = app.screen
        modal.query_one("#export-scope", Select).value = "all"
        modal.query_one("#export-path", Input).value = str(tmp_path / "all.csv")
        await pilot.pause()
        assert "4 all row(s)" in str(modal.query_one("#export-summary", Static).content)
        await save(pilot, app)
        assert read_csv(tmp_path / "all.csv") == serialized_rows(result, range(4))

    run_grid(scenario, result)


def test_open_export_keeps_snapshot_when_result_and_nested_values_change(tmp_path):
    result = RunOutcome(ok=True, columns=["value"], rows=[[[1, 2]]], total=1)

    async def scenario(app, pilot, table):
        table.action_export()
        await pilot.pause()
        modal = app.screen
        result.rows[0][0].append(3)
        table.show_result(RunOutcome(ok=True, columns=["new"], rows=[[99]], total=1))
        destination = tmp_path / "snapshot.csv"
        modal.query_one("#export-path", Input).value = str(destination)
        await pilot.pause()
        await save(pilot, app)
        assert read_csv(destination) == [["value"], ["[1, 2]"]]

    run_grid(scenario, result)


def test_existing_file_requires_overwrite_and_escape_writes_nothing(tmp_path):
    destination = tmp_path / "existing.csv"
    destination.write_text("keep this file", encoding="utf-8")

    async def scenario(app, pilot, table):
        table.action_export()
        await pilot.pause()
        modal = app.screen
        modal.query_one("#export-path", Input).value = str(destination)
        await pilot.pause()
        await save(pilot, app)
        assert destination.read_text(encoding="utf-8") == "keep this file"
        assert modal.query_one("#export-overwrite", Button).display
        assert "Nothing was written" in str(modal.query_one("#export-message", Static).content)
        await pilot.press("escape")
        assert not isinstance(app.screen, ExportModal)
        assert destination.read_text(encoding="utf-8") == "keep this file"

        table.action_export()
        await pilot.pause()
        app.screen.query_one("#export-path", Input).value = str(destination)
        await pilot.pause()
        await save(pilot, app)
        await save(pilot, app, "#export-overwrite")
        assert read_csv(destination) == serialized_rows(app.result, range(4))

    run_grid(scenario)


def test_destination_change_revokes_overwrite_confirmation_and_cancel_does_not_create(tmp_path):
    first, second = tmp_path / "first.csv", tmp_path / "second.csv"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")

    async def scenario(app, pilot, table):
        table.action_export()
        await pilot.pause()
        modal = app.screen
        modal.query_one("#export-path", Input).value = str(first)
        await pilot.pause()
        await save(pilot, app)
        modal.query_one("#export-path", Input).value = str(second)
        await pilot.pause()
        assert not modal.query_one("#export-overwrite", Button).display
        await save(pilot, app)
        assert first.read_text(encoding="utf-8") == "first"
        assert second.read_text(encoding="utf-8") == "second"
        modal.query_one("#export-path", Input).value = str(tmp_path / "cancelled.csv")
        await pilot.pause()
        await pilot.click("#export-cancel")
        assert not isinstance(app.screen, ExportModal)
        assert not (tmp_path / "cancelled.csv").exists()

    run_grid(scenario)


def test_export_errors_stay_in_modal_and_header_only_result_can_be_exported(tmp_path):
    result = RunOutcome(ok=True, columns=['empty,"header"'], total=0)

    async def scenario(app, pilot, table):
        table.action_export()
        await pilot.pause()
        modal = app.screen
        assert "0 all row(s)" in str(modal.query_one("#export-summary", Static).content)
        modal.query_one("#export-path", Input).value = str(tmp_path / "missing" / "out.csv")
        await pilot.pause()
        await save(pilot, app)
        assert app.screen is modal
        assert "CSV export failed:" in str(modal.query_one("#export-message", Static).content)
        assert not (tmp_path / "missing").exists()
        destination = tmp_path / "headers.csv"
        modal.query_one("#export-path", Input).value = str(destination)
        await pilot.pause()
        await save(pilot, app)
        assert read_csv(destination) == [result.columns]

    run_grid(scenario, result)


def test_no_result_has_no_export_and_copy_does_not_destroy_clipboard():
    async def scenario(app, pilot, table):
        app.copy_to_clipboard("keep clipboard")
        table.action_copy_rows()
        table.action_export()
        await pilot.pause()
        assert not isinstance(app.screen, ExportModal)
        assert app.clipboard == "keep clipboard"

    run_grid(scenario, RunOutcome(status="Transaction committed", ok=True))
