"""Pages preserve unsaved targets; schema inspection never edits or runs a document."""
from __future__ import annotations

import asyncio
import sqlite3

from textual.widgets import Input, OptionList, TabbedContent, Tree

from d8r.tui.app import D8RApp
from d8r.tui.session import Session


def test_pages_preserve_drafts_carets_names_and_offline_targets(tmp_path, monkeypatch):
    directory = tmp_path / "state"
    app = D8RApp(Session(data_dir=directory))
    target = "snapshot:" + str((tmp_path / "unopened.sqlite").resolve())
    first_id = ""
    second_id = ""

    async def edit():
        nonlocal first_id, second_id
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            app.editor.load_text("\\from users\n\\select user_id")
            app.editor.move_cursor((1, 8))
            first_id = app._document_identity
            app.action_toggle_pages()
            await pilot.pause()
            title = app.query_one("#page-title", Input)
            title.value = "Customer exploration"
            title.focus()
            await pilot.press("enter")
            await pilot.click("#page-new")
            await pilot.pause()
            second_id = app._document_identity
            assert second_id != first_id
            app.session.restore_source(target, "sqlite")
            app.editor.load_text("\\from private_table\n\\where unfinished")
            app.editor.move_cursor((1, 9))
            # No debounce pause: switching must snapshot the latest widget text.
            app.open_page(first_id)
            await pilot.pause()
            assert app.editor.text == "\\from users\n\\select user_id"
            assert app.editor.cursor_location == (1, 8)
            assert app.session.source_key() == "demo"
            assert app.query_one("#page-title", Input).value == "Customer exploration"
            app.open_page(second_id)
            app.exit()

    asyncio.run(edit())

    def forbidden(*args, **kwargs):
        raise AssertionError("page restoration must not connect or execute")

    monkeypatch.setattr(Session, "build_sqlite_source", forbidden)
    monkeypatch.setattr(Session, "run", forbidden)
    restored = D8RApp(Session(data_dir=directory))

    async def reopen():
        async with restored.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            assert restored._document_identity == second_id
            assert restored.editor.text == "\\from private_table\n\\where unfinished"
            assert restored.editor.cursor_location == (1, 9)
            assert restored.session.source_key() == target
            assert not restored.session.source_connected()
            assert restored.query_one("#explorer-tabs", TabbedContent).active == "tab-pages"
            assert restored.query_one("#pages-list", OptionList).option_count == 2
            restored.open_page(first_id)
            assert restored.editor.text == "\\from users\n\\select user_id"
            assert restored._document_identity == first_id
            assert not restored.session.history

    asyncio.run(reopen())


def test_pages_commands_reveal_then_toggle_without_document_changes(tmp_path):
    app = D8RApp(Session(data_dir=tmp_path))

    async def drive():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            for command in ("pages", "queries"):
                app.editor.load_text("\\" + command)
                app.editor.move_cursor((0, len(command) + 1))
                app.editor.focus()
                app.palette.sync()
                await pilot.press("tab")
                await pilot.pause()
                assert app.editor.text == ""
                assert app.query_one("#explorer-tabs", TabbedContent).active == "tab-pages"
                assert app.pane_visible("schema") == (command == "pages")

    asyncio.run(drive())


def test_schema_expansion_shows_typed_distinct_values_without_query_side_effects(tmp_path):
    path = tmp_path / "values.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("create table items (category text)")
        db.executemany("insert into items values (?)", [(None,), ("NULL",), ("paid",), ("paid",)])
    session = Session(data_dir=tmp_path / "state")
    source = session.build_sqlite_source(str(path))
    session.register(source)
    app = D8RApp(session)

    async def drive():
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            document = app.editor.text
            tree = app.query_one("#schema-tree", Tree)
            dataset = tree.root.children[0]
            dataset.expand()
            await pilot.pause()
            column = dataset.children[0]
            tree.focus()
            tree.move_cursor(column)
            await pilot.press("enter")
            await pilot.pause()
            assert "category" in app.editor.text
            assert column.children[0].data["kind"] == "placeholder"
            app.editor.load_text(document)
            tree.focus()
            tree.move_cursor(column)
            await pilot.press("space")
            await app.workers.wait_for_complete()
            await pilot.pause()
            labels = [str(child.label) for child in column.children if child.data["kind"] == "value"]
            assert set(labels) == {"NULL", "'NULL'", "'paid'"}
            assert app.editor.text == document
            assert not session.history
            assert not session.busy

    asyncio.run(drive())
    with sqlite3.connect(path) as db:
        db.executemany("insert into items values (?)", [(f"v{i:04d}",) for i in range(session.value_cache_limit + 1)])
    session.clear_value_cache(source.id)
    values, capped = session.distinct_values(source, "items", "category")
    assert capped and len(values) == session.value_cache_limit
    assert "NULL" in values
