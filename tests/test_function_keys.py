"""Function editor shortcuts exercise Textual's real focus and binding chain."""

from __future__ import annotations

import asyncio

import pytest

from textual.widgets import DataTable, Input, TextArea

from d8r.tui.app import D8RApp
from d8r.tui.palette import CommandPalette


def test_function_tab_accepts_before_navigating_and_noop_offers_release_focus():
    async def scenario():
        app = D8RApp()
        async with app.run_test(size=(150, 58)) as pilot:
            app.action_fn()
            await pilot.pause()
            screen = app.screen
            assert app.function_mode
            body = screen.query_one("#fn-body", TextArea)
            palette = screen.query_one("#fn-palette", CommandPalette)
            body.load_text("\\from events\n\\select us")
            body.move_cursor((1, len("\\select us")))
            body.focus()
            await pilot.pause()
            assert palette.view.labels == ["user_id"]

            await pilot.press("tab")
            assert body.text == "\\from events\n\\select user_id "
            assert app.focused is body
            assert palette.is_open

            # A visible exact-match offer must not trap Tab in the body.
            await pilot.press("tab")
            assert body.text == "\\from events\n\\select user_id "
            assert app.focused is screen.query_one("#fn-args", Input)
            assert not palette.is_open

            body.focus()
            await pilot.press("tab")
            assert app.focused is screen.query_one("#fn-args", Input)
            name = screen.query_one("#fn-name", Input)
            name.focus()
            await pilot.press("tab")
            assert app.focused is screen.query_one("#fn-doc", Input)

    asyncio.run(scenario())


def test_function_ctrl_c_copies_selected_body_without_leaving_or_editing():
    async def scenario():
        app = D8RApp()
        async with app.run_test(size=(150, 58)) as pilot:
            app.action_fn()
            await pilot.pause()
            screen = app.screen
            body = screen.query_one("#fn-body", TextArea)
            document = "\\from events\n\\select amount"
            body.load_text(document)
            body.move_cursor((1, len("\\select amount")))
            body.focus()
            await pilot.pause()
            await pilot.press(*(["shift+left"] * len("amount")))
            assert body.selected_text == "amount"

            await pilot.press("ctrl+c")
            assert app.clipboard == "amount"
            assert app.screen is screen
            assert app.focused is body
            assert body.text == document

            await pilot.press("ctrl+a", "ctrl+c")
            assert app.clipboard == document
            assert app.screen is screen
            assert body.text == document

    asyncio.run(scenario())


def test_function_ctrl_q_returns_to_live_workspace_and_preserves_unsaved_draft():
    async def scenario():
        app = D8RApp()
        async with app.run_test(size=(150, 58)) as pilot:
            workspace = app.screen
            original = app.editor.text
            app.action_fn()
            await pilot.pause()
            screen = app.screen
            body = screen.query_one("#fn-body", TextArea)
            draft = "\\from events\n\\select amount"
            body.load_text(draft)
            body.focus()
            await pilot.pause()

            await pilot.press("ctrl+q")
            await pilot.pause()
            assert app.screen is workspace
            assert app.focused is app.editor
            assert app.editor.text == original
            assert app.session.workspace["function_draft"]["body"] == draft
            assert not app.session.fns

            # Continuing to type proves Ctrl+Q did not shut down the app.
            await pilot.press("ctrl+a", "x")
            assert app.editor.text == "x"

    asyncio.run(scenario())


def test_workspace_completes_and_runs_direct_parameterized_function():
    async def scenario():
        app = D8RApp()
        app.session.save_fn("fn_top_n_free", "n", "\\from users\n\\select user_id\n"
                            "\\order user_id\n\\limit @n", "Top users")
        async with app.run_test(size=(150, 58)) as pilot:
            app.editor.load_text("\\fn_top_n_fr")
            app.editor.move_cursor((0, len(app.editor.text)))
            app.editor.focus()
            await pilot.pause()
            await pilot.press("tab", "2", "5", "right")
            assert app.editor.text == "\\fn_top_n_free(25)"
            await pilot.press("ctrl+enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.query_one("#results-table", DataTable).row_count == 25
            assert app.session.history[0].doc == "\\fn_top_n_free(25)"
            # Source-clause navigation recognizes the direct call rather than
            # inserting another source and silently replacing the function.
            app.editor.focus()
            await pilot.press("end", "enter", "\\", "f", "r", "o", "tab")
            assert app.editor.text == "\\fn_top_n_free(25)\n"
            assert app.editor.cursor_location == (0, len("\\fn_top_n_free(25)"))

            app.editor.load_text("\\select user_id \\fn_top_n_fr")
            app.editor.move_cursor((0, len(app.editor.text)))
            await pilot.pause()
            await pilot.press("tab", "2", "right", "ctrl+enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.editor.text == "\\select user_id \n\\fn_top_n_free(2)"
            assert app.query_one("#results-table", DataTable).row_count == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("document", ["\\fn", "\\fn top", "\\fn top_users"])
def test_fn_palette_tab_completes_saved_call_without_opening_editor(document):
    async def scenario():
        app = D8RApp()
        app.session.save_fn("top_users", "n", "\\from users\n\\select user_id\n"
                            "\\order user_id\n\\limit @n", "Top users")
        async with app.run_test(size=(150, 58)) as pilot:
            workspace = app.screen
            app.editor.load_text("")
            app.editor.focus()
            await pilot.press(*("space" if char == " " else char for char in document))
            await pilot.press("tab")
            assert app.screen is workspace
            assert app.focused is app.editor
            assert app.editor.text == "\\from top_users()"
            assert app.editor.cursor_location == (0, len("\\from top_users("))
            await pilot.press("2", "right", "ctrl+enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            table = app.query_one("#results-table", DataTable)
            assert [str(table.get_cell_at((row, 0))) for row in range(table.row_count)] == ["1", "2"]

    asyncio.run(scenario())


def test_function_shortcuts_work_from_explorer_and_preview_slots():
    async def scenario():
        app = D8RApp()
        app.session.save_fn("two_users", "", "\\from users\n\\limit 2", "")
        async with app.run_test(size=(140, 45)) as pilot:
            original = app.editor.text
            app._open_fn(focus="two_users")
            await pilot.pause()
            app.query_one("#fn-list").focus()
            await pilot.press("ctrl+r")
            await pilot.pause()
            grid = app.query_one("#fn-grid", DataTable)
            assert grid.row_count == 2
            grid.focus()
            await pilot.press("ctrl+d")
            assert "two_users" not in app.session.fns
            assert not app.session.history
            await pilot.press("ctrl+q")
            assert not app.function_mode
            assert app.editor.text == original

    asyncio.run(scenario())
