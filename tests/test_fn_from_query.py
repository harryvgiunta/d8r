"""Query-to-function drafts stay local to the form until an explicit save."""

from __future__ import annotations

import asyncio
import sqlite3

from textual.widgets import Input, OptionList, Select, TextArea

from d8r.engine import add_sqlite_source
from d8r.tui.app import D8RApp
from d8r.tui.fn import FnScreen, NEW
from d8r.tui.session import Session


async def pick_function(screen, pilot, name):
    listing = screen.query_one("#fn-list", OptionList)
    listing.highlighted = listing.get_option_index(name)
    listing.focus()
    await pilot.press("enter")
    await pilot.pause()


def test_query_draft_saves_explicitly_and_calls_on_the_captured_source(tmp_path):
    path = tmp_path / "readings.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("create table readings (id integer, amount integer)")
        connection.executemany("insert into readings values (?, ?)", [(1, 10), (2, 30)])
    session = Session()
    session.register(add_sqlite_source("readings", str(path), "Readings"))
    session.run("\\from readings\n\\select id")
    history = list(session.history)
    document = "\\from readings\n\\where amount > 20\n\\select id, amount\n"

    async def scenario():
        app = D8RApp(session)
        async with app.run_test(size=(150, 58)) as pilot:
            app.editor.load_text(document)
            await pilot.pause()
            app.action_query_to_fn()
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, FnScreen)
            assert screen.query_one("#fn-body", TextArea).text == document
            assert screen.query_one("#fn-source", Select).value == "readings"
            assert app.focused is screen.query_one("#fn-name", Input)
            assert not session.fns
            assert not session.storage_path.exists()
            assert session.history == history

            screen.query_one("#fn-name", Input).value = "large_readings"
            await pilot.click("#fn-save")
            await pilot.pause()
            assert session.fns["large_readings"].body == document
            assert not session.fns["large_readings"].params
            assert session.storage_path.exists()
            assert session.history == history
            assert session.active_id == "readings"
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert app.editor.text == document
            assert session.history == history

    asyncio.run(scenario())
    restored = Session(session.sources)
    restored.set_active("readings")
    outcome = restored.run("\\from large_readings()\n\\select *")
    assert outcome.error == ""
    assert outcome.columns == ["id", "amount"]
    assert outcome.rows == [[2, 30]]


def test_button_draft_neither_infers_parameters_nor_saves_invalid_body_on_cancel():
    session = Session()
    session.run("\\from events\n\\limit 1")
    history = list(session.history)
    document = "\\from events\n\\where amount > @minimum\n\\select user_id"

    async def scenario():
        app = D8RApp(session)
        async with app.run_test(size=(150, 58)) as pilot:
            app.editor.load_text(document)
            await pilot.pause()
            await pilot.click("#query-to-fn")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, FnScreen)
            assert screen.query_one("#fn-body", TextArea).text == document
            assert screen.query_one("#fn-params", Input).value == ""
            screen.query_one("#fn-name", Input).value = "threshold"
            await pilot.click("#fn-save")
            await pilot.pause()
            assert not session.fns
            assert not session.storage_path.exists()
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert app.editor.text == document
            assert session.history == history
            assert session.active_id == "demo"
            assert not session.storage_path.exists()

    asyncio.run(scenario())


def test_palette_action_copies_the_query_not_its_invocation():
    document = "\\from events\n\\select user_id\n\\limit 2\n"

    async def scenario():
        app = D8RApp()
        async with app.run_test(size=(150, 58)) as pilot:
            app.editor.load_text(document + "\\Query")
            app.editor.move_cursor((3, len("\\Query")))
            await pilot.pause()
            view = app.palette.view
            index = next(i for i, entry in enumerate(view.entries) if entry.action == "query-to-fn")
            app.palette.highlighted = index
            await pilot.press("enter")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, FnScreen)
            assert screen.query_one("#fn-body", TextArea).text == document
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert app.editor.text == document
            assert app.session.history == []
            assert not app.session.storage_path.exists()

    asyncio.run(scenario())


def test_prefill_never_overwrites_saved_entries_or_later_new_drafts():
    session = Session()
    saved_body = "\\from events\n\\select event_type\n\\limit 1"
    document = "\\from events\n\\select amount\n\\limit 2"
    session.save_fn("saved_events", "", saved_body, "")
    persisted = session.storage_path.read_bytes()

    async def scenario():
        app = D8RApp(session)
        async with app.run_test(size=(150, 58)) as pilot:
            app.editor.load_text(document)
            await pilot.pause()
            app.action_query_to_fn()
            await pilot.pause()
            screen = app.screen
            assert screen.query_one("#fn-body", TextArea).text == document
            await pick_function(screen, pilot, "saved_events")
            assert screen.query_one("#fn-body", TextArea).text == saved_body
            await pick_function(screen, pilot, NEW)
            assert screen.query_one("#fn-body", TextArea).text == ""
            assert screen.query_one("#fn-name", Input).value == ""
            await pilot.press("ctrl+c")
            await pilot.pause()

            app.push_screen(FnScreen(app, focus="saved_events", new_body=document))
            await pilot.pause()
            screen = app.screen
            assert screen.query_one("#fn-body", TextArea).text == saved_body
            await pick_function(screen, pilot, NEW)
            assert screen.query_one("#fn-body", TextArea).text == ""
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert app.editor.text == document
            assert session.fns["saved_events"].body == saved_body
            assert session.storage_path.read_bytes() == persisted
            assert session.history == []

    asyncio.run(scenario())
