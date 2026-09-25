"""Configured sample bounds stay local to each read-only AI context."""
from __future__ import annotations

import asyncio
import json

from anyql.ai import context as context_module
from anyql.ai.client import AIConfig
from anyql.ai.context import AIContext
from anyql.tui.session import Session


def test_configured_sample_default_and_requested_limits_return_bounded_rows(sources):
    session = Session(sources)
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
