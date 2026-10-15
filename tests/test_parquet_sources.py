"""Local Parquet loading stays lazy, read-only and scoped to a durable workspace."""

import asyncio
import json
import os
from threading import Event

import ibis
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from ibis.backends.duckdb import Backend
from textual.app import App
from textual.widgets import DataTable, Input, Select

from d8r.engine.datasources import capabilities_for
from d8r.engine.parquet import add_parquet_source
from d8r.tui.add_source import AddSourceModal
from d8r.tui.app import D8RApp
from d8r.tui.connection import build_saved_source, release_source
from d8r.tui.session import Session


@pytest.fixture
def parquet_file(tmp_path):
    path = tmp_path / "my own rows.pq"
    pq.write_table(pa.table({"id": [1, 2, 3], "label": ["one", "two", "three"]}), path)
    return path


def test_parquet_metadata_is_lazy_and_queries_use_real_rows(parquet_file, tmp_path, monkeypatch):
    before = parquet_file.read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("Discovery must not execute a query or materialize a table")

    with monkeypatch.context() as patch:
        patch.setattr(Backend, "execute", forbidden)
        patch.setattr(Backend, "create_table", forbidden)
        source = add_parquet_source(str(parquet_file), "My file")
    try:
        assert source.kind == "parquet"
        assert source.datasets["data"]["rows"] is None
        assert capabilities_for(source)["supports"]["temp"]
        assert capabilities_for(source)["supports"]["transactions"]
        session = Session({source.id: source}, data_dir=tmp_path / "state")
        outcome = session.run("\\from data\n\\where id > 1\n\\select id, label")
        assert outcome.ok, outcome.error
        assert outcome.rows == [[2, "two"], [3, "three"]]
        assert session.run("\\begin").ok
        assert session.run("\\from data\n\\limit 1\n\\temp kept").ok
        assert session.run("\\rollback").ok
        assert "kept" not in source.datasets
        assert parquet_file.read_bytes() == before
    finally:
        release_source(source)


@pytest.mark.parametrize("path", [
    "", "https://example.org/file.parquet", "s3://bucket/file.parquet",
    "file:///tmp/file.parquet", "//server/share/file.parquet", r"\\server\share\file.parquet",
    "*.parquet", "rows[1].parquet", "file.csv", "bad\x00.parquet",
])
def test_parquet_rejects_nonlocal_or_invalid_inputs_without_opening(path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid paths must not open a backend")
    monkeypatch.setattr(ibis.duckdb, "connect", forbidden)
    with pytest.raises(ValueError):
        add_parquet_source(path)


def test_parquet_missing_directory_and_corrupt_file_do_not_create_or_modify(tmp_path):
    missing = tmp_path / "missing.parquet"
    directory = tmp_path / "folder.parquet"
    directory.mkdir()
    corrupt = tmp_path / "invalid.parquet"
    corrupt.write_bytes(b"not parquet")
    for path in (missing, directory, corrupt):
        with pytest.raises(ValueError):
            add_parquet_source(str(path))
    assert not missing.exists()
    assert corrupt.read_bytes() == b"not parquet"


def test_parquet_identity_and_workspace_reopen(sources, parquet_file, tmp_path, monkeypatch):
    state = tmp_path / "state"
    session = Session(sources, data_dir=state)
    source = session.build_parquet_source(str(parquet_file), "Review")
    session.register(source)
    key = "parquet:" + os.path.normcase(str(parquet_file.resolve()))
    assert session.source_key() == key
    document = "\\from data\n\\select label\n\\limit 2"
    session.save_workspace(document=document, source=key, dialect="duckdb")
    saved = json.loads(session.workspace_path.read_text())
    assert saved["sources"][key] == {
        "id": key, "display": "Review", "kind": "parquet", "path": str(parquet_file.resolve()),
    }
    assert not session.backend_profiles
    monkeypatch.chdir(parquet_file.parent)
    same = add_parquet_source("./" + parquet_file.name, "Other label")
    try:
        assert same.connection_key == key
    finally:
        release_source(same)
        release_source(source)
    restored = Session(sources, data_dir=state)
    assert restored.source_key() == key
    assert not restored.source_connected()
    assert restored.workspace["document"] == document
    assert not restored.history
    connected = build_saved_source(restored, profile=restored.saved_source_profile(key))
    try:
        restored.register(connected)
        assert restored.source.display == "Review"
        assert restored.run(document).rows == [["one"], ["two"]]
        restored.set_active("demo")
        assert not restored.history
    finally:
        release_source(connected)


def test_parquet_modal_test_and_cancel_never_save(sources, parquet_file, tmp_path):
    state = tmp_path / "state"
    session = Session(sources, data_dir=state)
    original = tuple(session.sources)

    async def scenario():
        app = App()
        async with app.run_test(size=(140, 45)) as pilot:
            modal = AddSourceModal(session)
            await app.push_screen(modal)
            modal.query_one("#source-backend", Select).value = "parquet"
            modal.query_one("#parquet-path", Input).value = str(parquet_file)
            await pilot.pause()
            assert modal.query_one("#parquet-fields").display
            assert not modal.query_one("#d1-fields").display
            modal.action_test()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert modal._built is not None
            assert tuple(session.sources) == original
            assert not session.workspace_path.exists()
            assert not session.storage_path.exists()
            modal.action_cancel()
            await pilot.pause()
            assert modal._built is None
            assert tuple(session.sources) == original
            assert not session.workspace_path.exists()
            assert not session.storage_path.exists()

    asyncio.run(scenario())


@pytest.mark.parametrize("missing", [False, True])
def test_ctrl_o_add_query_and_startup_reopen(sources, parquet_file, tmp_path, missing):
    state = tmp_path / "state"
    session = Session(sources, data_dir=state)
    app = D8RApp(session)
    document = "\\from data\n\\select id, label\n\\limit 2"

    async def add_and_run():
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.press("ctrl+o")
            modal = app.screen
            assert isinstance(modal, AddSourceModal)
            modal.query_one("#source-backend", Select).value = "parquet"
            modal.query_one("#parquet-path", Input).value = str(parquet_file)
            modal.query_one("#display-name", Input).value = "Review rows"
            await pilot.pause()
            modal.action_add()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert session.source.kind == "parquet"
            assert app.query_one("#schema-tree").root.children
            app.editor.load_text(document)
            await pilot.press("ctrl+enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.query_one("#results-table", DataTable).row_count == 2
            assert len(session.history) == 1
            app.exit()

    asyncio.run(add_and_run())
    target = session.source_key()
    if missing:
        parquet_file.unlink()
    reopened_session = Session(sources, data_dir=state)
    assert not reopened_session.source_connected()
    reopened = D8RApp(reopened_session)

    async def restore():
        async with reopened.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            await reopened.workers.wait_for_complete()
            await pilot.pause()
            assert reopened_session.source_key() == target
            assert reopened.editor.text == document
            assert reopened_session.source_connected() is not missing
            assert reopened.query_one("#results-table", DataTable).row_count == 0
            assert len(reopened_session.history) == 1
            if missing:
                await pilot.press("ctrl+enter")
                await reopened.workers.wait_for_complete()
                await pilot.pause()
                assert len(reopened_session.history) == 1
                assert reopened.query_one("#results-table", DataTable).row_count == 0
                assert not parquet_file.exists()
                assert reopened_session.source_key() == target
            else:
                assert reopened_session.source.display == "Review rows"
                assert reopened.query_one("#schema-tree").root.children
                await pilot.press("ctrl+enter")
                await reopened.workers.wait_for_complete()
                await pilot.pause()
                assert len(reopened_session.history) == 2
                grid = reopened.query_one("#results-table", DataTable)
                assert [[str(grid.get_cell_at((row, column))) for column in range(2)]
                        for row in range(grid.row_count)] == [["1", "one"], ["2", "two"]]

    asyncio.run(restore())


def test_selecting_saved_parquet_reconnects_without_running_or_rewriting(sources, parquet_file, tmp_path):
    state = tmp_path / "state"
    first = Session(sources, data_dir=state)
    source = first.build_parquet_source(str(parquet_file), "Saved file")
    first.register(source, activate=False)
    target = source.connection_key
    first.save_workspace(document="\\from events\\limit 1", source="demo", dialect="duckdb")
    release_source(source)
    session = Session(sources, data_dir=state)
    assert ("Saved file · disconnected", target) in session.source_options()
    app = D8RApp(session)

    async def scenario():
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            assert session.active_id == "demo"
            assert target not in session.sources
            app.select_source(target)
            document = "\\from data\n\\select label"
            app.editor.load_text(document)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert session.source_connected()
            assert session.source_key() == target
            assert app.editor.text == document
            assert not session.history
            assert app.query_one("#results-table", DataTable).row_count == 0
            await pilot.press("ctrl+enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.query_one("#results-table", DataTable).row_count == 3
            app.select_source("demo")
            assert not session.history
            app.select_source(target)
            assert app.editor.text == document
            assert len(session.history) == 1

    asyncio.run(scenario())


def test_cancelled_parquet_add_disposes_late_real_source(sources, parquet_file, tmp_path, monkeypatch):
    session = Session(sources, data_dir=tmp_path / "state")
    started, release, closed = Event(), Event(), Event()
    build = session.build_parquet_source

    def delayed(*args, **kwargs):
        source = build(*args, **kwargs)
        disconnect = source.con.disconnect

        def close():
            disconnect()
            closed.set()

        source.con.disconnect = close
        started.set()
        if not release.wait(10):
            close()
            raise RuntimeError("Builder was not released")
        return source

    monkeypatch.setattr(session, "build_parquet_source", delayed)

    async def scenario():
        app = App()
        async with app.run_test(size=(140, 45)) as pilot:
            modal = AddSourceModal(session)
            await app.push_screen(modal)
            modal.query_one("#source-backend", Select).value = "parquet"
            modal.query_one("#parquet-path", Input).value = str(parquet_file)
            await pilot.pause()
            modal.action_add()
            try:
                assert await asyncio.to_thread(started.wait, 3)
                modal.action_cancel()
                release.set()
                assert await asyncio.to_thread(closed.wait, 3)
                assert not any(source.kind == "parquet" for source in session.sources.values())
                assert not session.workspace_path.exists()
                assert not session.storage_path.exists()
            finally:
                release.set()

    asyncio.run(scenario())
