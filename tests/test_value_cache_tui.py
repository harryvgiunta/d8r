"""On-demand value reads must not block D1 Run or publish stale suggestions."""

import asyncio
import json
import sqlite3
from threading import Event
from types import SimpleNamespace

import httpx
import pytest
from textual.widgets import DataTable, Static, Tree

from d8r.engine import CloudflareD1, add_d1_live_source
from d8r.tui.app import D8RApp
from d8r.tui.session import Session


@pytest.fixture
def remote_values(sources, tmp_path):
    state = SimpleNamespace(started=Event(), release=Event(), queried=Event(), reads=0)

    def handle(request):
        sql = json.loads(request.content)["sql"]
        distinct = "DISTINCT" in sql.upper()
        if distinct:
            state.reads += 1
            state.started.set()
            if not state.release.wait(10):
                raise RuntimeError("test did not release its value read")
        with sqlite3.connect(":memory:") as db:
            db.executescript("""
                create table items (id integer, category text);
                insert into items values (1, 'paid'), (2, 'new'), (3, null), (4, 'paid');
            """)
            cursor = db.execute(sql)
            columns = [column[0] for column in cursor.description]
            rows = cursor.fetchall()
            if request.url.path.endswith("/raw"):
                result = {"columns": columns, "rows": rows}
                if not distinct:
                    state.queried.set()
            else:
                result = [dict(zip(columns, row)) for row in rows]
        return httpx.Response(200, json={"success": True, "result": [{"success": True, "results": result}]})

    client = CloudflareD1("account", "test-secret", "11111111-2222-3333-4444-555555555555",
                          _client=httpx.Client(transport=httpx.MockTransport(handle)))
    source = add_d1_live_source("live", "account", "test-secret", client.database, client=client)
    session = Session(sources, data_dir=tmp_path)
    session.register(source)
    session.sync_d1_schema(source, client.schemas())
    state.session, state.source = session, source
    yield state
    state.release.set()
    client.close()


def complete_categories(app, suffix=""):
    line = "\\where category = " + suffix
    app.editor.load_text("\\from items\n" + line)
    app.editor.move_cursor((1, len(line)))
    app.palette.sync()


async def category_node(app, pilot):
    tree = app.query_one("#schema-tree", Tree)
    dataset = next(node for node in tree.root.children if node.data["name"] == "items")
    dataset.expand()
    await pilot.pause()
    return next(node for node in dataset.children if node.data["name"] == "category")


def test_completion_and_explorer_share_one_read_without_blocking_d1_run(remote_values):
    state = remote_values

    async def scenario():
        app = D8RApp(state.session)
        try:
            async with app.run_test(size=(140, 45)) as pilot:
                await pilot.pause()
                assert state.reads == 0  # full schema metadata never seeded column values
                complete_categories(app)
                assert await asyncio.to_thread(state.started.wait, 2)
                column = await category_node(app, pilot)
                column.expand()
                await pilot.pause()
                assert state.reads == 1
                assert not state.session.busy
                await pilot.press("p")
                assert app.editor.text.endswith("p")
                app.editor.load_text("\\from items\n\\select id\n\\limit 1")
                await pilot.press("f5")
                assert await asyncio.to_thread(state.queried.wait, 2), "Run waited for value suggestions"
                async with asyncio.timeout(2):
                    while app.run_busy:
                        await pilot.pause()
                status = str(app.query_one("#status", Static).content)
                assert str(app.query_one("#results-table", DataTable).get_cell_at((0, 0))) == "1"
                state.release.set()
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert str(app.query_one("#status", Static).content) == status
                assert len(state.session.history) == 1
                column = await category_node(app, pilot)
                column.expand()
                await pilot.pause()
                labels = {str(node.label) for node in column.children if node.data["kind"] == "value"}
                assert labels == {"NULL", "'new'", "'paid'"}
                complete_categories(app, "'pa")
                await pilot.pause()
                assert app.palette.view.labels == ["paid"]
                assert state.reads == 1
        finally:
            state.release.set()

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["escape", "source", "clear", "disable"])
def test_late_value_response_cannot_reopen_or_refill_invalidated_state(remote_values, change):
    state = remote_values

    async def scenario():
        app = D8RApp(state.session)
        try:
            async with app.run_test(size=(140, 45)) as pilot:
                complete_categories(app)
                assert await asyncio.to_thread(state.started.wait, 2)
                if change == "escape":
                    await pilot.press("escape")
                elif change == "source":
                    app.select_source("demo")
                    app.editor.load_text("\\from events\n\\limit 2")
                    app.editor.move_cursor((1, len("\\limit 2")))
                elif change == "clear":
                    app.clear_value_cache()
                else:
                    assert app.update_settings(value_cache_enabled=False)
                document = app.editor.text
                state.release.set()
                await app.workers.wait_for_complete()
                await pilot.pause()
                assert not app.palette.is_open
                assert app.editor.text == document
                assert not state.session.history
                if change in {"clear", "disable"}:
                    assert state.session.cached_value_columns == 0
                if change == "source":
                    assert state.session.active_id == "demo"
                    assert state.session.values_for("\\from events", "event_type") == []
        finally:
            state.release.set()

    asyncio.run(scenario())


def test_disabled_cache_keeps_only_current_completion_response(remote_values):
    state = remote_values
    state.release.set()
    state.session.update_settings(value_cache_enabled=False)

    async def scenario():
        app = D8RApp(state.session)
        async with app.run_test(size=(140, 45)) as pilot:
            complete_categories(app)
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert set(app.palette.view.labels) == {"new", "paid"}
            assert state.reads == 1
            assert state.session.cached_value_columns == 0
            await pilot.press("p")
            await pilot.pause()
            assert app.palette.view.labels == ["paid"]
            assert state.reads == 1
            app.editor.load_text("\\from items\n\\select id")
            app.editor.move_cursor((1, len("\\select id")))
            await pilot.pause()
            complete_categories(app)
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert state.reads == 2
            assert set(app.palette.view.labels) == {"new", "paid"}

    asyncio.run(scenario())
