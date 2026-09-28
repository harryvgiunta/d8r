"""Restart-level coverage for autosaved drafts and disconnected target restoration."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from textual.widgets import DataTable, Input, Select, Static, TextArea

from d8r.tui.add_source import AddSourceModal
from d8r.tui.app import D8RApp
from d8r.tui.session import Session


def _forbidden(*args, **kwargs):
    raise AssertionError("Restoring a workspace must not execute, connect, compile, or save a function")


def _snapshot(session, tmp_path):
    path = tmp_path / "events.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("create table events (id integer)")
        connection.execute("insert into events values (123)")
    source = session.build_sqlite_source(str(path), "Original snapshot label")
    session.register(source, activate=False)
    return source, path


def test_document_restart_flushes_last_edit_and_restores_missing_live_target(tmp_path, monkeypatch):
    data_dir = tmp_path / "state"
    first = Session(data_dir=data_dir)
    first.run("\\from events\n\\limit 1")
    first.remember_d1("a" * 32, "00000000-0000-0000-0000-000000000001", "Production")
    target = f"saved-d1:{'a' * 32}:00000000-0000-0000-0000-000000000001"
    first.restore_source(target, "sqlite")
    app = D8RApp(first)
    document = "\\from events\n\\select id\n\\limit 7"

    identity = None
    async def edit():
        nonlocal identity
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            app.editor.load_text(document)
            app.editor.move_cursor((1, 8))
            identity = app._ai_target().identity
            # Exit before the debounce timer or TextArea change messages run.
            app.exit()

    asyncio.run(edit())
    for name in ("build_live_source", "build_sqlite_source", "run", "compile", "save_fn"):
        monkeypatch.setattr(Session, name, _forbidden)
    reopened = D8RApp(Session(data_dir=data_dir))

    async def restore():
        async with reopened.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            assert reopened.editor.text == document
            assert reopened.editor.cursor_location == (1, 8)
            assert reopened.session.source_key() == target
            assert reopened.query_one("#source-select", Select).value == reopened.session.active_id
            assert "disconnected" in reopened.session.source.display
            assert str(reopened.query_one("#backend-pill", Static).content) == "disconnected"
            assert not reopened.query_one("#schema-tree").root.children
            assert reopened.query_one("#dialect-select", Select).value == "sqlite"
            assert reopened.query_one("#history-table", DataTable).row_count == 1
            assert reopened.query_one("#results-table", DataTable).row_count == 0
            assert reopened.query_one("#sql-text", TextArea).text == ""
            assert reopened._ai_target().identity == identity

    asyncio.run(restore())


def test_history_restores_snapshot_offline_and_run_reconnects_automatically(tmp_path, monkeypatch):
    data_dir = tmp_path / "state"
    first = Session(data_dir=data_dir)
    source, path = _snapshot(first, tmp_path)
    app = D8RApp(first)
    document = "\\from events\n\\select id"

    async def run_and_leave():
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            app.select_source(source.id)
            app.editor.load_text(document)
            app.action_run()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert str(app.query_one("#results-table", DataTable).get_cell_at((0, 0))) == "123"
            app.select_source("demo")
            app.editor.load_text("\\from users")
            app.exit()

    asyncio.run(run_and_leave())
    connections = []
    build = Session.build_sqlite_source

    def record_connection(self, snapshot_path, display=""):
        connections.append(snapshot_path)
        return build(self, snapshot_path, display)

    monkeypatch.setattr(Session, "build_sqlite_source", record_connection)
    monkeypatch.setattr(Session, "build_live_source", _forbidden)
    reopened = D8RApp(Session(data_dir=data_dir))

    async def restore_history():
        async with reopened.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            assert reopened.session.active_id == "demo"
            table = reopened.query_one("#history-table", DataTable)
            assert table.row_count == 1
            old_identity = reopened._ai_target().identity
            reopened.query_one("#result-tabs").active = "tab-history"
            table.focus()
            await pilot.press("enter")
            await pilot.pause()
            assert reopened.editor.text == document
            assert reopened._ai_target().identity != old_identity
            target = reopened.session.source_key()
            assert target == f"snapshot:{path.resolve()}"
            assert not reopened.session.source_connected()
            assert not reopened.query_one("#schema-tree").root.children
            assert not connections
            assert len(reopened.session.history) == 1
            reopened.action_compile()
            assert "disconnected" in str(reopened.query_one("#status", Static).content).lower()
            identity = reopened._ai_target().identity
            await pilot.press("f5")
            await pilot.pause()
            await reopened.workers.wait_for_complete()
            await pilot.pause()
            assert not isinstance(reopened.screen, AddSourceModal)
            assert connections == [str(path.resolve())]
            assert reopened.session.source_connected()
            assert reopened.session.source_key() == target
            assert reopened._ai_target().identity == identity
            assert reopened.query_one("#source-select", Select).value == reopened.session.active_id
            assert "disconnected" not in str(reopened.query_one("#backend-pill", Static).content)
            assert str(reopened.query_one("#results-table", DataTable).get_cell_at((0, 0))) == "123"
            assert len(reopened.session.history) == 2
            assert reopened.query_one("#schema-tree").root.children
            reopened.action_run()
            await reopened.workers.wait_for_complete()
            await pilot.pause()
            assert str(reopened.query_one("#results-table", DataTable).get_cell_at((0, 0))) == "123"
            assert connections == [str(path.resolve())]
            assert len(reopened.session.history) == 3

    asyncio.run(restore_history())


def test_function_restart_restores_unsaved_draft_without_saving_definition(tmp_path, monkeypatch):
    data_dir = tmp_path / "state"
    first = Session(data_dir=data_dir)
    original = "\\from events\n\\limit 1"
    first.save_fn("saved_events", "", original, "Original definition")
    source, _ = _snapshot(first, tmp_path)
    app = D8RApp(first)
    body = "\\from events\n\\where id > @minimum\n\\select id"
    identity = None

    async def edit_draft():
        nonlocal identity
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            app._open_fn(focus="saved_events")
            await pilot.pause()
            screen = app.function_editor
            screen.query_one("#fn-source", Select).value = source.id
            await pilot.pause()
            screen.query_one("#fn-name", Input).value = "draft_events"
            screen.query_one("#fn-doc", Input).value = "Unpublished description"
            screen.query_one("#fn-params", Input).value = "minimum"
            screen.query_one("#fn-args", Input).value = "100"
            editor = screen.query_one("#fn-body", TextArea)
            editor.load_text(body)
            editor.move_cursor((1, 15))
            identity = screen._ai_target().identity
            app.exit()

    asyncio.run(edit_draft())
    for name in ("build_live_source", "build_sqlite_source", "run", "compile", "save_fn"):
        monkeypatch.setattr(Session, name, _forbidden)
    reopened = D8RApp(Session(data_dir=data_dir))

    async def restore_and_close():
        async with reopened.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            screen = reopened.function_editor
            assert reopened.function_mode
            assert screen.query_one("#fn-name", Input).value == "draft_events"
            assert screen.query_one("#fn-doc", Input).value == "Unpublished description"
            assert screen.query_one("#fn-params", Input).value == "minimum"
            assert screen.query_one("#fn-args", Input).value == "100"
            assert screen.query_one("#fn-body", TextArea).text == body
            assert screen.query_one("#fn-body", TextArea).cursor_location == (1, 15)
            assert screen._ai_target().identity == identity
            assert not reopened.session.source_connected(screen.source_id)
            assert "disconnected" in reopened.session.sources[screen.source_id].display
            assert reopened.session.active_id == "demo"
            assert reopened.session.fns["saved_events"].body == original
            assert "draft_events" not in reopened.session.fns
            assert not reopened.session.history
            assert screen.preview_grid.row_count == 0
            await pilot.press("ctrl+q")
            await pilot.pause()
            assert not reopened.function_mode

    asyncio.run(restore_and_close())
    third = D8RApp(Session(data_dir=data_dir))

    async def workspace_then_new_draft():
        async with third.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            assert not third.function_mode
            third.editor.load_text("")
            third.action_query_to_fn()
            await pilot.pause()
            screen = third.function_editor
            assert third.function_mode
            assert screen.query_one("#fn-name", Input).value == ""
            assert screen.query_one("#fn-body", TextArea).text == ""
            assert screen._ai_target().identity != identity
            assert screen.source_id == "demo"

    asyncio.run(workspace_then_new_draft())


def test_autosave_failure_keeps_editor_available_and_retries_on_exit(tmp_path, monkeypatch):
    data_dir = tmp_path / "state"
    app = D8RApp(Session(data_dir=data_dir))
    document = "\\from users\n\\select name"

    def fail_save(document):
        raise ValueError("Workspace is not writable; choose a writable data directory.")

    async def edit_with_write_failure():
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            with monkeypatch.context() as patch:
                patch.setattr(app.session._workspace, "save", fail_save)
                app.editor.load_text(document)
                await pilot.pause(0.3)
                assert app.editor.text == document
                assert "not writable" in str(app.query_one("#status", Static).content)
                app.editor.move_cursor((1, 12))
                app.editor.insert("_edited")
                await pilot.pause(0.3)
                assert app.editor.text == document + "_edited"
            app.exit()

    asyncio.run(edit_with_write_failure())
    reopened = D8RApp(Session(data_dir=data_dir))

    async def restore_recovered_draft():
        async with reopened.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            assert reopened.editor.text == document + "_edited"

    asyncio.run(restore_recovered_draft())


def test_saved_function_is_offered_at_unchanged_workspace_caret(tmp_path):
    app = D8RApp(Session(data_dir=tmp_path / "state"))
    prefix = "\\from quick_"

    async def scenario():
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            app.editor.load_text(prefix)
            app.editor.move_cursor((0, len(prefix)))
            await pilot.pause()
            app.action_fn()
            await pilot.pause()
            screen = app.screen
            screen.query_one("#fn-name", Input).value = "quick_events"
            screen.query_one("#fn-body", TextArea).load_text("\\from events\n\\limit 1")
            await pilot.click("#fn-save")
            await pilot.pause()
            await pilot.press("ctrl+q")
            await pilot.pause()
            assert app.editor.text == prefix
            assert app.editor.cursor_location == (0, len(prefix))
            assert app.focused is app.editor
            assert app.palette.is_open
            assert app.palette.view.labels == ["quick_events()"]
            await pilot.press("tab")
            assert app.editor.text == "\\from quick_events()"
            app.action_run()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.query_one("#results-table", DataTable).row_count == 1
            assert not str(app.query_one("#results-error", Static).content)

    asyncio.run(scenario())


@pytest.mark.parametrize(("suppression", "line"), [("disabled", "\\from "), ("dismissed", "\\from "), ("dismissed", "\\")])
def test_returning_from_function_editor_respects_suppressed_completion(tmp_path, suppression, line):
    app = D8RApp(Session(data_dir=tmp_path / "state"))

    async def scenario():
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            app.editor.load_text(line)
            app.editor.move_cursor((0, len(line)))
            await pilot.pause()
            assert app.palette.is_open
            if suppression == "disabled":
                app.update_settings(intellisense=False)
            else:
                await pilot.press("escape")
            app.action_fn()
            await pilot.pause()
            screen = app.screen
            screen.query_one("#fn-name", Input).value = "quick_events"
            screen.query_one("#fn-body", TextArea).load_text("\\from events\n\\limit 1")
            await pilot.click("#fn-save")
            await pilot.pause()
            await pilot.press("ctrl+q")
            await pilot.pause()
            assert not app.palette.is_open
            assert app.editor.text == line
            assert app.editor.cursor_location == (0, len(line))

    asyncio.run(scenario())
