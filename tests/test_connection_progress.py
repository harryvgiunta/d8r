"""Connection deadlines must revoke ownership, not merely hide progress."""

import asyncio
import json
import sqlite3
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import httpx
import pytest
from textual.app import App, ComposeResult
from textual.widgets import DataTable, Input, LoadingIndicator, ProgressBar, Static

from d8r.engine import CloudflareD1, DataSource
from d8r.tui.add_source import AddSourceModal
from d8r.tui.connection import ConnectionProgress, ConnectionStatus, build_connection
from d8r.tui.session import Session


@pytest.fixture
def pending_source():
    started, release, closed = Event(), Event(), Event()
    disconnects = []

    def disconnect():
        disconnects.append(True)
        closed.set()

    source = DataSource(
        id="pending", display="orders", doc="", kind="d1", dialect="sqlite", dir=Path("orders.sqlite"),
        con=SimpleNamespace(disconnect=disconnect),
    )

    def build(*args, **kwargs):
        started.set()
        if not release.wait(5):
            raise RuntimeError("test did not release its builder")
        return source

    yield SimpleNamespace(
        build=build, source=source, started=started, release=release, closed=closed, disconnects=disconnects,
    )
    release.set()


def test_timeout_closes_late_result_without_transferring_it(pending_source):
    pending = pending_source

    async def scenario():
        with pytest.raises(TimeoutError):
            await build_connection(pending.build, timeout=0.01)
        assert pending.started.is_set()
        assert not pending.closed.is_set()
        pending.release.set()
        assert await asyncio.to_thread(pending.closed.wait, 2)
        assert pending.disconnects == [True]

    asyncio.run(scenario())


def test_cancelled_builder_cleans_up_even_after_event_loop_shutdown(pending_source):
    pending = pending_source

    async def scenario():
        task = asyncio.create_task(build_connection(pending.build))
        assert await asyncio.to_thread(pending.started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    pending.release.set()
    assert pending.closed.wait(2)
    assert pending.disconnects == [True]


class ConnectionApp(App):
    def compose(self) -> ComposeResult:
        yield Input(id="editor")


class YieldingConnectionProgress(ConnectionProgress):
    async def on_mount(self, event):
        event.prevent_default()
        super().on_mount()
        # Real mount dispatch may yield before Textual marks the widget mounted.
        await asyncio.sleep(0)
        await asyncio.sleep(0)


@pytest.mark.parametrize("outcome", ["connected", "timeout"])
def test_connection_starts_after_yielding_mount_and_always_settles(pending_source, outcome):
    pending = pending_source
    if outcome == "connected":
        pending.release.set()

    async def scenario():
        settled = asyncio.Event()
        connected, failures = [], []

        def complete(source):
            connected.append(source)
            settled.set()

        def failed(message):
            failures.append(message)
            settled.set()

        app = ConnectionApp()
        async with app.run_test(size=(100, 30)) as pilot:
            progress = YieldingConnectionProgress(
                "orders", pending.build, complete, valid=lambda: True, failed=failed,
                timeout=0.05 if outcome == "timeout" else 2,
            )
            await app.screen.mount(progress)
            await asyncio.wait_for(settled.wait(), 2)
            await pilot.pause()
            assert pending.started.is_set()
            assert not app.query(ConnectionProgress)
            if outcome == "connected":
                assert connected == [pending.source] and not failures
            else:
                assert not connected and "timed out" in failures[0]
                pending.release.set()
                assert await asyncio.to_thread(pending.closed.wait, 2)

    asyncio.run(scenario())


def test_background_connection_keeps_editor_usable_and_discards_stale_target(pending_source):
    pending = pending_source
    connected, failed = [], []
    valid = True

    async def scenario():
        nonlocal valid
        app = ConnectionApp()
        async with app.run_test(size=(100, 30)) as pilot:
            screen = app.screen
            progress = ConnectionProgress(
                "orders", pending.build, connected.append, valid=lambda: valid, failed=failed.append,
            )
            await screen.mount(progress)
            assert await asyncio.to_thread(pending.started.wait, 2)
            assert app.screen is screen
            app.query_one("#editor", Input).focus()
            await pilot.press("a", "b", "c")
            assert app.query_one("#editor", Input).value == "abc"
            valid = False
            pending.release.set()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert connected == []
            assert failed == []
            assert pending.disconnects == [True]
            assert not app.query(ConnectionProgress)

    asyncio.run(scenario())


def test_background_timeout_reports_failure_and_cannot_connect_later(pending_source):
    pending = pending_source
    connected, failures = [], []

    async def scenario():
        failed = asyncio.Event()

        def failure(message):
            failures.append(message)
            failed.set()

        app = ConnectionApp()
        async with app.run_test(size=(100, 30)) as pilot:
            progress = ConnectionProgress(
                "orders", pending.build, connected.append, valid=lambda: True, failed=failure, timeout=0.05,
            )
            await app.screen.mount(progress)
            await asyncio.wait_for(failed.wait(), 2)
            assert "timed out" in failures[0]
            pending.release.set()
            assert await asyncio.to_thread(pending.closed.wait, 2)
            await pilot.pause()
            assert connected == []
            assert pending.disconnects == [True]
            assert not app.query(ConnectionProgress)

    asyncio.run(scenario())


def test_add_timeout_restores_form_without_saving_and_retry_can_succeed(sources, tmp_path, monkeypatch, pending_source):
    pending = pending_source
    session = Session(sources, data_dir=tmp_path)
    monkeypatch.setattr(session, "build_live_source", pending.build)
    monkeypatch.setattr(AddSourceModal, "CONNECTION_TIMEOUT", 0.05)
    transferred = []

    async def scenario():
        app = ConnectionApp()
        async with app.run_test(size=(140, 45)) as pilot:
            modal = AddSourceModal(session)
            await app.push_screen(modal, transferred.append)
            modal.query_one("#account-id", Input).value = "account"
            modal.query_one("#database", Input).value = "orders"
            modal.query_one("#api-token", Input).value = "secret-token"
            await pilot.pause()
            modal.action_add()
            assert not modal.query_one("#add-source").display
            assert modal.query_one(ConnectionStatus).display
            await app.workers.wait_for_complete()
            assert modal.query_one("#add-source").display
            assert not modal.query_one(ConnectionStatus).display
            assert "timed out" in str(modal.query_one("#add-source-message", Static).content)
            assert transferred == []
            assert not session.storage_path.exists()
            pending.release.set()
            assert await asyncio.to_thread(pending.closed.wait, 2)
            assert pending.disconnects == [True]
            # The timed-out attempt never becomes the modal's cached Test result.
            replacement = DataSource(
                id="retry", display="retry", doc="", kind="d1", dialect="sqlite", dir=Path("retry.sqlite"),
            )
            monkeypatch.setattr(session, "build_live_source", lambda *args: replacement)
            modal.action_add()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert transferred == [replacement]
            assert not session.storage_path.exists()

    asyncio.run(scenario())


@pytest.mark.parametrize("interrupt", ["cancel", "new-page", "switch"])
def test_interrupted_startup_connection_cannot_register_or_execute_late(
    sources, tmp_path, monkeypatch, pending_source, interrupt,
):
    from d8r.tui.app import D8RApp

    pending = pending_source
    session = Session(sources, data_dir=tmp_path)
    path = str((tmp_path / "orders.sqlite").resolve())
    target = "snapshot:" + path
    session.save_workspace(sources={target: {
        "id": "orders", "display": "orders", "kind": "d1", "path": path,
    }})
    session.restore_source(target, "sqlite")
    monkeypatch.setattr(session, "build_sqlite_source", pending.build)
    app = D8RApp(session)

    async def scenario():
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            app.editor.load_text("\\from orders")
            assert await asyncio.to_thread(pending.started.wait, 2)
            await pilot.pause()
            assert not isinstance(app.screen, AddSourceModal)
            if interrupt == "cancel":
                await pilot.click(".connection-cancel")
            elif interrupt == "new-page":
                app._new_page()
            else:
                app.select_source("demo")
            app.editor.load_text("replacement draft")
            pending.release.set()
            assert await asyncio.to_thread(pending.closed.wait, 2)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert session.source_key() == ("demo" if interrupt == "switch" else target)
            assert not session.source_connected(target)
            assert not session.history
            assert app.editor.text == "replacement draft"
            assert pending.disconnects == [True]

    asyncio.run(scenario())


@pytest.fixture
def indexing_d1(sources, tmp_path, monkeypatch):
    """Real D1 request/SQLite execution, with full discovery held independently."""
    state = SimpleNamespace(started=Event(), release=Event(), finished=Event(),
                            raw=Event(), fail=False, requests=[])

    def handler(request):
        sql = json.loads(request.content)["sql"]
        state.requests.append(sql)
        full_index = "sqlite_schema" in sql and "m.name in (" not in sql
        if full_index:
            state.query_preceded_index = state.raw.is_set()
            state.started.set()
            if not state.release.wait(10):
                raise RuntimeError("test did not release schema indexing")
            if state.fail:
                state.finished.set()
                return httpx.Response(200, json={"success": True, "result": [{
                    "success": False, "errors": [{"message": "index denied smoke-secret"}],
                }]})
        with sqlite3.connect(":memory:") as database:
            database.executescript("""
                create table orders (id integer, amount integer);
                insert into orders values (7, 12);
                create table archive (id integer, amount integer);
                insert into archive values (8, 24);
            """)
            cursor = database.execute(sql)
            columns = [column[0] for column in cursor.description]
            rows = cursor.fetchall()
            if request.url.path.endswith("/raw"):
                results = {"columns": columns, "rows": rows}
                state.raw.set()
            else:
                results = [dict(zip(columns, row)) for row in rows]
        if full_index:
            state.finished.set()
        return httpx.Response(200, json={"success": True, "result": [{"success": True, "results": results}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("d8r.engine.datasources.CloudflareD1", lambda **kwargs: CloudflareD1(**kwargs, _client=client))
    session = Session(sources, data_dir=tmp_path)
    session.remember_d1("account", "11111111-2222-3333-4444-555555555555", "Saved D1", "smoke-secret")
    target = next(key for _, key in session.source_options() if key.startswith("saved-d1:"))
    session.restore_source(target, "sqlite")
    state.session = session
    yield state
    state.release.set()
    client.close()


async def run_while_indexing(app, pilot, state, document):
    assert await asyncio.to_thread(state.started.wait, 3), "startup did not start schema discovery"
    state.raw.clear()
    app.editor.load_text(document)
    await pilot.press("f5")
    assert await asyncio.to_thread(state.raw.wait, 3), "query waited for full schema discovery"
    async with asyncio.timeout(3):
        while app.run_busy:
            await pilot.pause()
    await pilot.pause()


def test_run_and_compile_do_not_wait_for_full_schema_index(indexing_d1):
    from d8r.tui.app import D8RApp

    state = indexing_d1

    async def scenario():
        app = D8RApp(state.session)
        async with app.run_test(size=(140, 45)) as pilot:
            assert await asyncio.to_thread(state.started.wait, 2)
            assert not state.raw.is_set()  # Startup connects and indexes, but never runs a document.
            assert not state.session.history
            assert app.query_one("#results-table", DataTable).row_count == 0
            await run_while_indexing(app, pilot, state, "\\from orders\n\\select id")
            assert not state.release.is_set()
            assert app.query_one("#schema-index").display
            assert app.query_one("#schema-index-dots", LoadingIndicator).display
            assert not app.query(ProgressBar)
            assert str(app.query_one("#results-table", DataTable).get_cell_at((0, 0))) == "7"
            assert not state.session.schema.tables_complete
            assert state.session.schema.table_by_name("orders") is not None
            assert state.session.schema.table_by_name("archive") is None
            assert not state.session.busy
            before = list(state.requests)
            assert state.session.compile("\\from orders\n\\select id")[0] is not None
            assert state.session.compile("\\from archive\n\\select id")[0] is None
            assert state.requests == before  # compile never does hidden metadata IO

            # Once some columns are known, another not-yet-indexed table must
            # still run, including through saved-function expansion.
            state.session.save_fn("archived", "", "\\from archive\n\\select id", "")
            await run_while_indexing(app, pilot, state, "\\from archived()\n\\select id")
            assert str(app.query_one("#results-table", DataTable).get_cell_at((0, 0))) == "8"
            app.editor.load_text("unchanged draft while schema loads")
            app.editor.move_cursor((0, 9))
            state.release.set()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert state.session.schema.tables_complete
            assert {table.name for table in state.session.schema.tables} == {"orders", "archive"}
            assert not app.query_one("#schema-index").display
            assert app.editor.text == "unchanged draft while schema loads"
            assert app.editor.cursor_location == (0, 9)
            assert len(state.session.history) == 2
            assert str(app.query_one("#results-table", DataTable).get_cell_at((0, 0))) == "8"

    try:
        asyncio.run(scenario())
    finally:
        state.release.set()


def test_failed_background_index_does_not_fail_queries(indexing_d1):
    from d8r.tui.app import D8RApp

    state = indexing_d1
    state.fail = True

    async def scenario():
        app = D8RApp(state.session)
        async with app.run_test(size=(140, 45)) as pilot:
            await run_while_indexing(app, pilot, state, "\\from orders\n\\select id")
            status = str(app.query_one("#status", Static).content)
            state.release.set()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert "failed" in str(app.query_one("#schema-index-label", Static).content)
            assert "smoke-secret" not in str(app.query_one("#schema-index-label", Static).content)
            assert not app.query_one("#schema-index-dots").display
            assert str(app.query_one("#status", Static).content) == status
            await run_while_indexing(app, pilot, state, "\\from archive\n\\select id")
            assert str(app.query_one("#results-table", DataTable).get_cell_at((0, 0))) == "8"
            assert len(state.session.history) == 2
            assert not state.session.schema.tables_complete

    try:
        asyncio.run(scenario())
    finally:
        state.release.set()


@pytest.mark.parametrize("change", ["switch", "replace", "exit"])
def test_late_schema_index_respects_source_ownership(indexing_d1, change):
    from d8r.tui.app import D8RApp

    state = indexing_d1

    async def scenario():
        app = D8RApp(state.session)
        async with app.run_test(size=(140, 45)) as pilot:
            await run_while_indexing(app, pilot, state, "\\from orders\n\\select id")
            assert await asyncio.to_thread(state.started.wait, 2)
            source = state.session.source
            if change == "exit":
                app.exit()
            else:
                app.select_source("demo")
                if change == "replace":
                    replacement = DataSource(id=source.id, display="replacement", doc="", kind="d1-live",
                                             dialect="sqlite", dir=Path("replacement"))
                    state.session.register(replacement, activate=False)
                state.release.set()
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert state.session.source.id == "demo"
                assert state.session.schema.table_by_name("events") is not None
                assert state.session.schema.table_by_name("orders") is None
                assert not app.query_one("#schema-index").display
                assert len(state.session.history) == 1
                if change == "replace":
                    assert not replacement.datasets
                    assert not source.schema_indexed
                else:
                    assert source.schema_indexed
        if change == "exit":
            state.release.set()
            assert await asyncio.to_thread(state.finished.wait, 2)
            await asyncio.sleep(0)
            assert not source.schema_indexed
            assert set(source.datasets) == {"orders"}

    try:
        asyncio.run(scenario())
    finally:
        state.release.set()
