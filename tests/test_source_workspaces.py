"""Source switches restore independent pages, function drafts and agent rosters."""
from __future__ import annotations

import asyncio

from textual.widgets import DataTable, Input, OptionList, Select, TabbedContent, TextArea

from d8r.tui.app import D8RApp
from d8r.tui.session import Session
from d8r.tui.results import ResultsTable


def _option_ids(app, selector):
    listing = app.query_one(selector, OptionList)
    return {listing.get_option_at_index(index).id for index in range(listing.option_count)}


def test_source_workspace_switch_and_restart_preserve_local_state(tmp_path):
    session = Session(data_dir=tmp_path)
    session.save_fn("local", "", "\\from events", "Demo function")
    with session.target_source("mysql"):
        session.save_fn("local", "", "\\from products", "MySQL function")
    app = D8RApp(session)
    identities = {}

    async def edit():
        async with app.run_test(size=(150, 48)) as pilot:
            await pilot.pause()
            app.editor.load_text("\\from events\n\\select id")
            app.editor.move_cursor((1, 9))
            identities["demo"] = app._document_identity
            app.action_ai()
            app.query_one("#ai-input", TextArea).load_text("Demo question draft")
            app.ai_panel._flush_chat()
            demo_chat = app.ai_panel.chat
            app._open_fn()
            app.function_editor.query_one("#fn-name", Input).value = "demo_unsaved"
            app.function_editor.query_one("#fn-body", TextArea).load_text("\\from users")
            app.action_workspace()
            app._new_page()
            app.editor.load_text("\\from users\n\\limit 3")
            app.editor.move_cursor((1, 7))
            identities["demo_last"] = app._document_identity
            app.query_one("#source-select", Select).value = "mysql"
            await pilot.pause()
            assert app.editor.text == ""
            assert app._document_identity not in identities.values()
            assert app.session.fns["local"].body == "\\from products"
            assert not set(identities.values()) & _option_ids(app, "#pages-list")
            assert demo_chat.id not in _option_ids(app, "#agents-list")
            app.editor.load_text("\\from products\n\\select id")
            identities["mysql"] = app._document_identity
            app._open_fn()
            assert app.function_editor.query_one("#fn-name", Input).value == ""
            app.function_editor.query_one("#fn-name", Input).value = "mysql_unsaved"
            # The function form source picker uses the same workspace transition.
            app.function_editor.query_one("#fn-source", Select).value = "demo"
            await pilot.pause()
            assert app.session.active_id == "demo"
            assert app._document_identity == identities["demo_last"]
            assert app.editor.text == "\\from users\n\\limit 3"
            assert app.editor.cursor_location == (1, 7)
            assert app.session.fns["local"].body == "\\from events"
            assert _option_ids(app, "#pages-list") == {identities["demo"], identities["demo_last"]}
            assert demo_chat.id in _option_ids(app, "#agents-list")
            app._open_fn()
            assert app.function_editor.query_one("#fn-name", Input).value == "demo_unsaved"
            assert app.function_editor.query_one("#fn-body", TextArea).text == "\\from users"
            app.action_workspace()
            app.open_page(identities["demo"])
            assert app.editor.text == "\\from events\n\\select id"
            assert app.ai_panel.chat.id == demo_chat.id
            assert app.query_one("#ai-input", TextArea).text == "Demo question draft"
            app.select_source("mysql")
            assert app.function_mode
            assert app.function_editor.query_one("#fn-name", Input).value == "mysql_unsaved"
            assert not session.history
            app.exit()

    asyncio.run(edit())
    restored = D8RApp(Session(data_dir=tmp_path))

    async def reopen():
        async with restored.run_test(size=(150, 48)) as pilot:
            await pilot.pause()
            assert restored.session.active_id == "mysql"
            assert restored._document_identity == identities["mysql"]
            assert restored.editor.text == "\\from products\n\\select id"
            assert restored.function_mode
            assert restored.function_editor.query_one("#fn-name", Input).value == "mysql_unsaved"
            restored.select_source("demo")
            assert restored._document_identity == identities["demo"]
            assert restored.editor.text == "\\from events\n\\select id"
            restored._open_fn()
            assert restored.function_editor.query_one("#fn-name", Input).value == "demo_unsaved"
            assert not restored.session.history

    asyncio.run(reopen())


def test_deleting_last_source_page_never_opens_another_source(tmp_path):
    app = D8RApp(Session(data_dir=tmp_path))

    async def drive():
        async with app.run_test(size=(130, 42)) as pilot:
            await pilot.pause()
            demo_id = app._document_identity
            app.editor.load_text("\\from events")
            app.select_source("mysql")
            mysql_id = app._document_identity
            app.editor.load_text("\\from products")
            app.delete_page(mysql_id)
            assert app.session.active_id == "mysql"
            assert app.editor.text == ""
            assert app._document_identity not in {demo_id, mysql_id}
            assert demo_id not in _option_ids(app, "#pages-list")
            app.select_source("demo")
            assert app._document_identity == demo_id
            assert app.editor.text == "\\from events"

    asyncio.run(drive())


def test_history_results_sql_and_pins_stay_in_their_connection(tmp_path):
    session = Session(data_dir=tmp_path)
    app = D8RApp(session)

    async def drive():
        async with app.run_test(size=(150, 48)) as pilot:
            await pilot.pause()
            app.editor.load_text("\\from events\n\\limit 2")
            app.action_run()
            await app.workers.wait_for_complete()
            await pilot.pause()
            demo_page = app._document_identity
            demo_rows = app.query_one("#results-table", ResultsTable).raw_rows
            demo_sql = app.query_one("#sql-text", TextArea).text
            app._pin_main_result()
            await pilot.pause()
            pin = next(iter(app._data_tabs))
            app.select_source("mysql")
            await pilot.pause()
            assert app.query_one("#results-table", ResultsTable).raw_rows == ()
            assert app.query_one("#sql-text", TextArea).text == ""
            assert app.query_one("#history-table", DataTable).row_count == 0
            assert not app.query_one("#result-tabs", TabbedContent).get_tab(pin).display
            app.open_page(demo_page)
            app.delete_page(demo_page)
            assert session.active_id == "mysql"
            assert any(page["id"] == demo_page for page in session.workspace["pages"])
            app.editor.load_text("\\from products\n\\limit 1")
            app.action_run()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert [entry.source for entry in session.history] == ["mysql"]
            app.select_source("demo")
            await pilot.pause()
            assert app.query_one("#results-table", ResultsTable).raw_rows == demo_rows
            assert app.query_one("#sql-text", TextArea).text == demo_sql
            assert app.query_one("#history-table", DataTable).row_count == 1
            assert [entry.source for entry in session.history] == ["demo"]
            assert app.query_one("#result-tabs", TabbedContent).get_tab(pin).display

    asyncio.run(drive())
