"""AI drafts must bind to cached schemas without querying their data source."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from d8r.ai import context as context_module
from d8r.ai.context import AIContext, AIProposal
from d8r.query import FnDef
from d8r.tui.session import Session


@pytest.fixture
def offline_context(monkeypatch):
    session = Session()
    context = AIContext(session, "demo", "")

    def forbidden(*args, **kwargs):
        pytest.fail("Proposal validation must not contact or execute against a source")

    monkeypatch.setattr(session, "run", forbidden)
    monkeypatch.setattr(context_module, "execute", forbidden)
    monkeypatch.setattr(context_module, "execute_remote", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    for method in ("table", "list_tables", "execute", "raw_sql"):
        monkeypatch.setattr(session.source.con, method, forbidden)
    return context


@pytest.mark.parametrize("body", [
    "\\from events\n\\select missing_field",
    "\\from events\n\\where missing_field = 1",
    "\\from events\n\\select sum(missing_field)",
    "\\with selected\n  \\from events\n  \\select event_type as kind\n\\select event_type",
    "\\from ( \\from events \\select event_type as kind ) selected\n\\select event_type",
    "\\from events\n\\where user_id in ( \\from users \\select missing_field )",
    "\\from events\n\\select missing_field;\n\\from users\n\\select *",
])
def test_invalid_fields_fail_tool_and_replacement_gate(offline_context, body):
    result = json.loads(asyncio.run(offline_context.call_tool("validate_d8r", {"text": body})))
    assert result["valid"] is False
    assert result["error"]
    assert offline_context.validate_replacement(AIProposal(body)) is not None
    assert not offline_context.session.history


@pytest.mark.parametrize("body", [
    "\\with selected\n  \\from events\n  \\select event_type as kind\n\\select kind",
    "\\with selected\n  \\from events\n  \\select event_type as kind\n\\with counts\n  \\from selected\n  \\select kind, count(*) as total\n\\select kind, total\n\\order total desc",
    "\\from ( \\from events \\select event_type as kind ) selected\n\\select kind",
    "\\begin;\n\\rollback",
])
def test_cached_derived_fields_and_transaction_statements_validate(offline_context, body):
    result = json.loads(asyncio.run(offline_context.call_tool("validate_d8r", {"text": body})))
    assert result == {"valid": True, "error": None}
    assert offline_context.validate_replacement(AIProposal(body)) is None
    assert not offline_context.session.history


def test_unindexed_metadata_is_not_successful_validation(offline_context):
    offline_context.session.schema = replace(offline_context.session.schema, tables=(), tables_complete=False)
    body = "\\from unindexed\n\\select id"
    result = json.loads(asyncio.run(offline_context.call_tool("validate_d8r", {"text": body})))
    assert result["valid"] is False
    assert offline_context.validate_replacement(AIProposal(body)) is not None


@pytest.mark.parametrize(("column", "valid"), [("kind", True), ("event_type", False)])
def test_parameterless_functions_bind_cte_output_fields(offline_context, column, valid):
    body = ("\\with selected\n  \\from events\n  \\select event_type as kind\n"
            f"\\select {column}")
    result = json.loads(asyncio.run(offline_context.call_tool(
        "validate_d8r", {"text": body, "parameters": []},
    )))
    assert result["valid"] is valid
    proposal = AIProposal(body, FnDef("selected_events", (), body))
    assert (offline_context.validate_replacement(proposal) is None) is valid
    assert not offline_context.session.fns
    assert not offline_context.session.storage_path.exists()


def test_saved_function_expansion_cannot_hide_missing_fields(offline_context):
    session = offline_context.session
    session.save_fn("bad_fields", "n", "\\from events\n\\select missing_field\n\\limit @n", "")
    body = "\\from bad_fields(2)"
    result = json.loads(asyncio.run(offline_context.call_tool("validate_d8r", {"text": body})))
    assert result["valid"] is False
    assert offline_context.validate_replacement(AIProposal(body)) is not None


def test_temporal_function_parameters_validate_at_the_call_site(offline_context):
    body = "\\from events\n\\where timestamp >= @since\n\\select user_id"
    context = offline_context
    proposal = AIProposal(body, FnDef("since_events", ("since",), body))
    assert context.validate_replacement(proposal) is None
    context.session.save_fn("since_events", "since", body, "")
    assert context.validate_proposal("\\from since_events('2026-10-09')") is None
    assert context.validate_proposal("\\from since_events('not-a-date')") is not None


def test_unexpected_validation_errors_are_sanitized_and_rejected(offline_context, monkeypatch):
    def fail(*args):
        raise RuntimeError("password=secret backend detail")

    monkeypatch.setattr(context_module, "validate_schema", fail)
    body = "\\from events\n\\select event_type"
    result = json.loads(asyncio.run(offline_context.call_tool("validate_d8r", {"text": body})))
    assert result["valid"] is False
    error = offline_context.validate_replacement(AIProposal(body))
    assert error is not None
    assert "secret" not in error and "secret" not in result["error"]
