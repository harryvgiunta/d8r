"""Opt-in query checks execute bounded reads without changing the workspace."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from threading import Event

import pytest

from d8r.ai import context as context_module
from d8r.ai.client import AIConfig
from d8r.ai.context import AIContext
from d8r.tui.session import Session


QUERY = "\\from events\n\\select event_type\n\\order event_type"


def test_query_permission_is_opt_in_and_live_revocation_prevents_reads(sources, monkeypatch):
    session = Session(sources)
    disabled = AIContext(session, "demo", "")
    assert "test_query" not in {tool["function"]["name"] for tool in disabled.tools}
    session.update_settings(ai_test_queries=True)
    enabled = AIContext(session, "demo", "")
    assert "test_query" in {tool["function"]["name"] for tool in enabled.tools}
    session.update_settings(ai_test_queries=False)
    reads = []

    def forbidden(*args, **kwargs):
        reads.append((args, kwargs))
        raise AssertionError("A denied tool must not execute")

    monkeypatch.setattr(context_module, "execute", forbidden)
    monkeypatch.setattr(context_module, "execute_remote", forbidden)
    monkeypatch.setattr(session, "run", forbidden)

    async def scenario():
        for context in (disabled, enabled):
            result = json.loads(await context.call_tool("test_query", {"text": QUERY}))
            assert result["executed"] is False
            assert "error" in result
            assert not result.get("rows")

    asyncio.run(scenario())
    assert reads == []


def test_real_query_checks_bound_rows_and_honor_smaller_and_zero_limits(sources):
    session = Session(sources)
    session.update_settings(ai_test_queries=True, ai_allow_sample_data=True)
    session.ai_config = AIConfig(sample_rows=4)
    context = AIContext(session, "demo", "")

    async def scenario():
        default = json.loads(await context.call_tool("test_query", {"text": QUERY}))
        assert default["executed"] is True
        assert default["row_count"] == 4
        assert default["has_rows"] is True
        assert default["columns"] == ["event_type"]
        assert default["rows"] == [["click"]] * 4
        for limit, expected in ((2, 2), (0, 0), (100, 4)):
            result = json.loads(await context.call_tool("test_query", {"text": f"{QUERY}\n\\limit {limit}"}))
            assert result["executed"] is True
            assert result["row_count"] == expected
            assert result["has_rows"] is bool(expected)
            assert result["columns"] == ["event_type"]
            assert result["rows"] == [["click"]] * expected

    asyncio.run(scenario())


def test_query_evidence_does_not_require_sample_permission(sources):
    session = Session(sources)
    session.update_settings(ai_test_queries=True)
    session.ai_config = AIConfig(sample_rows=3)
    context = AIContext(session, "demo", "")

    async def scenario():
        private = json.loads(await context.call_tool("test_query", {"text": QUERY}))
        assert private["executed"] is True
        assert private["row_count"] == 3
        assert private["has_rows"] is True
        assert private["columns"] == ["event_type"]
        assert private.get("rows") is None
        session.update_settings(ai_allow_sample_data=True)
        shared = json.loads(await context.call_tool("test_query", {"text": QUERY}))
        assert shared["rows"] == [["click"]] * 3
        session.update_settings(ai_allow_sample_data=False)
        private_again = json.loads(await context.call_tool("test_query", {"text": QUERY}))
        assert private_again["executed"] is True
        assert private_again["row_count"] == 3
        assert private_again.get("rows") is None

    asyncio.run(scenario())


def test_empty_results_are_not_runtime_errors_and_engine_errors_are_sanitized(sources, monkeypatch):
    session = Session(sources)
    session.update_settings(ai_test_queries=True, ai_allow_sample_data=True)
    context = AIContext(session, "demo", "")
    real_execute = context_module.execute
    engine_errors = []

    def execute(*args, **kwargs):
        try:
            return real_execute(*args, **kwargs)
        except Exception as exc:
            engine_errors.append(exc)
            raise

    monkeypatch.setattr(context_module, "execute", execute)

    async def scenario():
        empty = json.loads(await context.call_tool("test_query", {
            "text": "\\from events\n\\select event_type\n\\where event_type = 'not-a-real-event-type'",
        }))
        assert empty["executed"] is True
        assert empty["row_count"] == 0
        assert empty["has_rows"] is False
        assert empty["columns"] == ["event_type"]
        assert empty["rows"] == []
        assert not empty.get("error")
        failed = json.loads(await context.call_tool("test_query", {
            "text": "\\from events\n\\select json_text('{private-malformed-json}', 'key') as value",
        }))
        assert engine_errors, "The malformed JSON must fail in the real engine, not syntax validation"
        assert failed["executed"] is False
        assert "error" in failed
        assert "private-malformed-json" not in json.dumps(failed)
        assert not failed.get("rows")

    asyncio.run(scenario())


@pytest.mark.parametrize("text", [
    "\\begin",
    "\\commit",
    "\\rollback",
    "\\savepoint before",
    "\\release before",
    "\\from events\n\\temp checked_events",
    "\\drop events",
    "\\from events;\n\\from users",
    "\\from events;\n\\drop events",
    "\\begin\n\\from events",
])
def test_query_checks_reject_non_readonly_documents_before_execution(sources, monkeypatch, text):
    session = Session(sources)
    session.update_settings(ai_test_queries=True)
    context = AIContext(session, "demo", "")
    reads = []

    def forbidden(*args, **kwargs):
        reads.append((args, kwargs))
        raise AssertionError("Rejected documents must not execute")

    monkeypatch.setattr(context_module, "execute", forbidden)
    monkeypatch.setattr(context_module, "execute_remote", forbidden)
    monkeypatch.setattr(session, "run", forbidden)
    result = json.loads(asyncio.run(context.call_tool("test_query", {"text": text})))
    assert result["executed"] is False
    assert "error" in result
    assert reads == []
    assert session.tx == {}
    assert session.temp_tables() == []
    assert "events" in session.sources["demo"].datasets
    assert session.history == []


def test_query_checks_preserve_an_existing_user_transaction(sources, monkeypatch):
    session = Session(sources)
    session.update_settings(ai_test_queries=True)
    assert session.run("\\begin").ok
    reads = []

    def forbidden(*args, **kwargs):
        reads.append((args, kwargs))
        raise AssertionError("AI checks must not risk aborting a user transaction")

    monkeypatch.setattr(context_module, "execute", forbidden)
    monkeypatch.setattr(context_module, "execute_remote", forbidden)
    try:
        context = AIContext(session, "demo", "")
        result = json.loads(asyncio.run(context.call_tool("test_query", {"text": QUERY})))
        assert result["executed"] is False
        assert "error" in result
        assert reads == []
        assert session.tx_state().open is True
        assert session.run("\\from events\n\\limit 1", record=False).ok
        assert session.tx_state().open is True
    finally:
        assert session.run("\\rollback", record=False).ok


def test_query_checks_keep_source_pinned_without_changing_history_or_workspace(sources, monkeypatch):
    session = Session(sources)
    session.update_settings(ai_test_queries=True, ai_allow_sample_data=True)
    previous = session.run("\\from events\n\\limit 1")
    assert previous.ok
    context = AIContext(session, "demo", QUERY)
    session.set_active("mysql")
    session.save_workspace(document="\\from products\n\\limit 1", cursor=[1, 2])
    workspace = deepcopy(session.workspace)
    history = {key: list(session.history_for(key)) for key in ("demo", "mysql")}

    def forbidden(*args, **kwargs):
        pytest.fail("Query checks must never call Session.run")

    monkeypatch.setattr(session, "run", forbidden)
    result = json.loads(asyncio.run(context.call_tool("test_query", {"text": f"{QUERY}\n\\limit 1"})))
    assert result["executed"] is True
    assert result["columns"] == ["event_type"]
    assert result["rows"] == [["click"]]
    assert session.active_id == "mysql"
    assert session.workspace == workspace
    assert {key: session.history_for(key) for key in history} == history


@pytest.mark.parametrize("change", ["query_permission", "sample_permission", "source"])
def test_changes_during_query_execution_discard_unapproved_evidence(sources, monkeypatch, change):
    session = Session(sources)
    session.update_settings(ai_test_queries=True, ai_allow_sample_data=True)
    context = AIContext(session, "demo", "")
    real_execute = context_module.execute
    release = Event()

    async def scenario():
        started = asyncio.Event()
        loop = asyncio.get_running_loop()

        def delayed_execute(*args, **kwargs):
            result = real_execute(*args, **kwargs)
            loop.call_soon_threadsafe(started.set)
            if not release.wait(5):
                raise TimeoutError("Query test did not release the engine worker")
            return result

        monkeypatch.setattr(context_module, "execute", delayed_execute)
        pending = asyncio.create_task(context.call_tool("test_query", {"text": f"{QUERY}\n\\limit 1"}))
        try:
            await asyncio.wait_for(started.wait(), 5)
            if change == "source":
                session.sources["demo"] = replace(session.sources["demo"])
            elif change == "query_permission":
                session.update_settings(ai_test_queries=False)
            else:
                session.update_settings(ai_allow_sample_data=False)
        finally:
            release.set()
        result = json.loads(await asyncio.wait_for(pending, 5))
        assert result.get("rows") is None
        if change == "sample_permission":
            assert result["executed"] is True
            assert result["row_count"] == 1
        else:
            assert result["executed"] is False
            assert "error" in result
            assert "row_count" not in result

    asyncio.run(scenario())
