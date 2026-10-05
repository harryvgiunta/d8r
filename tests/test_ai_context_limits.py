"""Configured sample bounds stay local to each read-only AI context."""
from __future__ import annotations

import asyncio
import json

from d8r.ai import context as context_module
from d8r.ai.client import AIConfig
from d8r.ai.context import AIContext
from d8r.tui.session import Session


def test_configured_sample_default_and_requested_limits_return_bounded_rows(sources):
    session = Session(sources)
    session.update_settings(ai_allow_sample_data=True)
    session.ai_config = AIConfig(sample_rows=12)
    context = AIContext(session, "demo", "")

    async def scenario():
        default = json.loads(await context.call_tool("sample_rows", {"table": "events"}))
        requested = json.loads(await context.call_tool("sample_rows", {"table": "events", "limit": 7}))
        minimum = json.loads(await context.call_tool("sample_rows", {"table": "events", "limit": 1}))
        assert len(default["rows"]) == 12
        assert len(requested["rows"]) == 7
        assert len(minimum["rows"]) == 1

    asyncio.run(scenario())


def test_sample_limits_reject_invalid_requests_before_reading(sources, monkeypatch):
    session = Session(sources)
    session.update_settings(ai_allow_sample_data=True)
    session.ai_config = AIConfig(sample_rows=12)
    context = AIContext(session, "demo", "")
    reads = []

    def unexpected_read(*args):
        reads.append(args)
        return {"columns": [], "rows": []}

    monkeypatch.setattr(context_module, "execute", unexpected_read)
    monkeypatch.setattr(context_module, "execute_remote", unexpected_read)

    async def scenario():
        for limit in (13, 0, True):
            result = json.loads(await context.call_tool("sample_rows", {"table": "events", "limit": limit}))
            assert "error" in result

    asyncio.run(scenario())
    assert reads == []


def test_simultaneous_contexts_freeze_independent_sample_caps(sources, monkeypatch):
    session = Session(sources)
    session.update_settings(ai_allow_sample_data=True)
    session.ai_config = AIConfig(sample_rows=3)
    small = AIContext(session, "demo", "")
    session.ai_config = AIConfig(sample_rows=11)
    large = AIContext(session, "demo", "")
    session.ai_config = AIConfig(sample_rows=20)

    def oversized_read(*args):
        # A backend returning excess rows must not escape the context's cap.
        return {"columns": ["value"], "rows": [[value] for value in range(30)]}

    monkeypatch.setattr(context_module, "execute", oversized_read)

    async def scenario():
        responses = await asyncio.gather(
            small.call_tool("sample_rows", {"table": "events"}),
            large.call_tool("sample_rows", {"table": "events"}),
            small.call_tool("sample_rows", {"table": "events", "limit": 4}),
            large.call_tool("sample_rows", {"table": "events", "limit": 4}),
        )
        small_default, large_default, small_excess, large_requested = map(json.loads, responses)
        assert small_default["rows"] == [[value] for value in range(3)]
        assert large_default["rows"] == [[value] for value in range(11)]
        assert "error" in small_excess
        assert large_requested["rows"] == [[value] for value in range(4)]

    asyncio.run(scenario())
    for context, cap in ((small, 3), (large, 11)):
        tool = next(tool["function"] for tool in context.tools if tool["function"]["name"] == "sample_rows")
        assert tool["parameters"]["properties"]["limit"]["maximum"] == cap


def test_read_permissions_block_tools_and_revoke_existing_context(sources, monkeypatch):
    session = Session(sources)
    default = AIContext(session, "demo", "")
    assert "sample_rows" not in {tool["function"]["name"] for tool in default.tools}

    async def scenario():
        assert "error" in json.loads(await default.call_tool("sample_rows", {"table": "events"}))
        schema = json.loads(await default.call_tool("schema", {"table": "events"}))
        assert schema["tables"][0]["name"] == "events"
        session.update_settings(ai_allow_sample_data=True)
        allowed = AIContext(session, "demo", "")
        sample = json.loads(await allowed.call_tool("sample_rows", {"table": "events", "limit": 1}))
        assert len(sample["rows"]) == 1

        session.update_settings(ai_allow_sample_data=False, ai_allow_schema_refreshes=False)
        reads = []

        def forbidden(*args):
            reads.append(args)

        monkeypatch.setattr(allowed, "_schema", forbidden)
        monkeypatch.setattr(allowed, "_sample", forbidden)
        assert "error" in json.loads(await allowed.call_tool("schema", {}))
        assert "error" in json.loads(await allowed.call_tool("sample_rows", {"table": "events"}))
        assert reads == []
        denied = AIContext(session, "demo", "")
        assert not {"schema", "sample_rows"} & {tool["function"]["name"] for tool in denied.tools}
        # Permissions do not remove unrelated tools or normal IDE execution.
        validation = json.loads(await denied.call_tool("validate_d8r", {"text": "\\from events"}))
        assert validation["valid"]
        assert session.run("\\from events\n\\limit 1").ok

    asyncio.run(scenario())


def test_disabling_samples_discards_inflight_records(sources, monkeypatch):
    session = Session(sources)
    session.update_settings(ai_allow_sample_data=True)
    context = AIContext(session, "demo", "")

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()
        original = context._sample

        async def delayed_sample(*args):
            rows = await original(*args)
            started.set()
            await release.wait()
            return rows

        monkeypatch.setattr(context, "_sample", delayed_sample)
        read = asyncio.create_task(context.call_tool("sample_rows", {"table": "events"}))
        await asyncio.wait_for(started.wait(), 5)
        session.update_settings(ai_allow_sample_data=False)
        release.set()
        result = json.loads(await read)
        assert "error" in result and "rows" not in result

    asyncio.run(scenario())
