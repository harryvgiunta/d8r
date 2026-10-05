"""Projected AI reads expose real JSON fields without widening read authority."""
from __future__ import annotations

import asyncio
import json

import ibis
import pytest

from d8r.ai import context as context_module
from d8r.ai.context import AIContext
from d8r.engine import DataSource
from d8r.tui.palette import view_for
from d8r.tui.session import Session


@pytest.fixture
def json_session(tmp_path):
    con = ibis.duckdb.connect()
    row = {f"field_{index}": index for index in range(24)}
    row["details_json"] = '{"synthetic_key":"visible"}'
    row["long_text"] = "x" * 600
    table = con.create_table("wide_records", ibis.memtable([row]))
    table = table.mutate(native_json=table.details_json.cast("json"))
    source = DataSource("demo", "Synthetic JSON", "", "demo", "duckdb", tmp_path,
                        con=con, datasets={"wide_records": {"table": table, "rows": 1, "doc": ""}})
    session = Session({"demo": source}, data_dir=tmp_path / "session")
    session.update_settings(ai_allow_sample_data=True)
    try:
        yield session
    finally:
        con.disconnect()


def test_projection_reads_actual_json_beyond_first_twenty_columns(json_session):
    context = AIContext(json_session, "demo", "")

    async def scenario():
        default = json.loads(await context.call_tool("sample_rows", {"table": "wide_records"}))
        selected = json.loads(await context.call_tool("sample_rows", {
            "table": "wide_records", "limit": 1, "columns": ["details_json", "field_23", "long_text"],
        }))
        assert "details_json" not in default["columns"]
        assert selected["columns"] == ["details_json", "field_23", "long_text"]
        assert selected["rows"] == [['{"synthetic_key":"visible"}', 23, "x" * 500]]

    asyncio.run(scenario())


def test_projection_rejects_unknown_duplicate_oversized_and_wrong_type_columns_before_read(json_session, monkeypatch):
    context = AIContext(json_session, "demo", "")
    reads = []

    def forbidden(*args):
        reads.append(args)
        raise AssertionError("Invalid projection must not execute")

    monkeypatch.setattr(context_module, "execute", forbidden)
    monkeypatch.setattr(context_module, "execute_remote", forbidden)

    async def scenario():
        for columns in ([], ["details_json", "details_json"], ["not_registered"],
                        [f"field_{index}" for index in range(21)], "details_json", [True], [1], [None], [{}]):
            result = json.loads(await context.call_tool("sample_rows", {
                "table": "wide_records", "columns": columns,
            }))
            assert "error" in result and "rows" not in result

    asyncio.run(scenario())
    assert reads == []


def test_projected_inflight_read_is_discarded_when_source_changes(json_session, monkeypatch):
    context = AIContext(json_session, "demo", "")

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        original = context._sample

        async def delayed(*args):
            result = await original(*args)
            started.set()
            await release.wait()
            return result

        monkeypatch.setattr(context, "_sample", delayed)
        pending = asyncio.create_task(context.call_tool("sample_rows", {
            "table": "wide_records", "columns": ["details_json"],
        }))
        await asyncio.wait_for(started.wait(), 5)
        json_session.sources.pop("demo")
        release.set()
        result = json.loads(await pending)
        assert "error" in result and "rows" not in result

    asyncio.run(scenario())


def test_json_completion_flows_through_numeric_and_argument_dependent_calls(json_session):
    line = "\\select json_text(coalesce("
    view = view_for(json_session, "\\from wide_records\n" + line, line, len(line))
    assert {"details_json", "native_json"} <= set(view.labels) and "field_0" not in view.labels
    assert "json_get" in view.labels
    line = "\\select field_0 / "
    view = view_for(json_session, "\\from wide_records\n" + line, line, len(line))
    assert {"json_int", "json_float"} <= set(view.labels)
    assert not {"json_get", "json_text", "json_bool"} & set(view.labels)
