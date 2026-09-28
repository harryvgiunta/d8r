"""Workspace function proposals are explicit, source-pinned and never auto-saved."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from textual.widgets import Button, TextArea

from d8r.ai import client
from d8r.ai.client import AIConfig
from d8r.ai.context import AIContext
from d8r.tui.app import D8RApp
from d8r.tui.session import Session


BODY = "\\from events\n\\where event_type = @kind\n\\limit 2"
OLD_BODY = "\\from events\n\\limit 1"


def _metadata(name="events_by_kind", *, original_name=None, parameters=None):
    return {"kind": "function", "original_name": original_name, "name": name,
            "description": "Events matching a kind", "parameters": ["kind"] if parameters is None else parameters,
            "arguments": "'click'"}


def _answer(metadata, body=BODY):
    return "```json\n" + json.dumps(metadata) + "\n```\n```d8r\n" + body + "\n```"


def _frame(text="", *, done=False):
    return ("data: " + json.dumps({"choices": [{"index": 0,
            "delta": {} if done else {"content": text},
            "finish_reason": "stop" if done else None}]}) + "\n\n").encode()


def _install(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(client, "create_client", lambda config: httpx.AsyncClient(transport=transport))


def _session():
    session = Session()
    session.ai_config = AIConfig("https://provider.invalid/v1", "test", "private-key", max_attempts=1)
    return session


async def _finished(panel):
    async def wait():
        while panel.busy:
            await asyncio.sleep(0.01)
    await asyncio.wait_for(wait(), 8)


def test_workspace_distinguishes_document_create_and_edit_without_mutation():
    session = Session()
    original = session.save_fn("saved", "", OLD_BODY, "Saved definition")
    stored = session.storage_path.read_bytes()
    context = AIContext(session, "demo", "\\from users")
    document = context.read_proposal(_answer({"kind": "document"}, OLD_BODY))
    created = context.read_proposal(_answer(_metadata()))
    edited = context.read_proposal(_answer(_metadata("saved", original_name="saved")))
    assert [item.kind for item in (document, created, edited)] == ["document", "function_create", "function_edit"]
    assert all(context.validate_replacement(item) is None for item in (document, created, edited))
    assert edited.function.params == ("kind",)
    assert session.fns == {"saved": original}
    assert session.storage_path.read_bytes() == stored
    assert not session.history


@pytest.mark.parametrize("metadata", [
    {"name": "missing_kind", "description": "Unsafe", "parameters": [], "arguments": ""},
    {**_metadata(), "original_name": ""},
    {**_metadata(), "original_name": False},
    {**_metadata(), "parameters": "kind"},
    {**_metadata(), "parameters": [None]},
    {**_metadata(), "overwrite": True},
    {"kind": "document", "name": "disguised_function"},
])
def test_malformed_function_metadata_cannot_fall_back_to_document(metadata):
    context = AIContext(Session(), "demo", "")
    with pytest.raises(ValueError):
        context.read_proposal(_answer(metadata))


def test_unclosed_metadata_does_not_become_a_document_replacement():
    context = AIContext(Session(), "demo", "")
    with pytest.raises(ValueError):
        context.read_proposal("```d8r\n" + OLD_BODY + "\n```\n```json\n{\"kind\": \"function\"")


@pytest.mark.parametrize("metadata", [
    _metadata("saved"),
    _metadata("renamed", original_name="saved"),
    _metadata("unknown", original_name="unknown"),
])
def test_existing_functions_require_explicit_edit_without_renaming(metadata):
    session = Session()
    original = session.save_fn("saved", "", OLD_BODY, "Original")
    context = AIContext(session, "demo", "")
    proposal = context.read_proposal(_answer(metadata))
    assert context.validate_replacement(proposal) is not None
    assert session.fns == {"saved": original}


def test_request_start_function_snapshot_survives_registry_updates():
    session = Session()
    session.save_fn("saved", "", OLD_BODY, "Original")
    context = AIContext(session, "demo", "")
    session.save_fn("saved", "kind", BODY, "Changed while waiting")
    session.save_fn("later", "", OLD_BODY, "Added while waiting")
    functions = json.loads(asyncio.run(context.call_tool("functions", {})))["functions"]
    assert functions == [{"name": "saved", "description": "Original", "parameters": [], "body": OLD_BODY}]
    edited = context.read_proposal(_answer(_metadata("saved", original_name="saved")))
    colliding = context.read_proposal(_answer(_metadata("later")))
    assert context.validate_replacement(edited) is not None
    assert context.validate_replacement(colliding) is not None


@pytest.mark.parametrize(("parameters", "body", "valid"), [
    (["kind"], BODY, True),
    ([], OLD_BODY, True),
    ([], BODY, False),
    (["kind", "kind"], BODY, False),
    (["kind,other"], BODY, False),
    ([""], OLD_BODY, False),
    (["@kind"], BODY, False),
    (["kind"], "\\from events\n\\temp forbidden", False),
])
def test_workspace_function_validation_checks_proposed_signature_without_execution(parameters, body, valid, monkeypatch):
    session = Session()
    context = AIContext(session, "demo", "")

    def never_run(*args, **kwargs):
        pytest.fail("Function validation must not execute a query")

    monkeypatch.setattr(session, "run", never_run)
    result = json.loads(asyncio.run(context.call_tool("validate_d8r", {"text": body, "parameters": parameters})))
    assert result["valid"] is valid
    proposal = context.read_proposal(_answer(_metadata(parameters=parameters), body))
    assert (context.validate_replacement(proposal) is None) is valid
    assert not session.fns and not session.history
    assert not session.storage_path.exists()


def test_function_draft_cannot_target_a_different_saved_function():
    session = Session()
    session.save_fn("selected", "", OLD_BODY, "Selected")
    session.save_fn("other", "", OLD_BODY, "Other")
    context = AIContext(session, "demo", OLD_BODY, parameters=(), function_name="selected")
    matching = context.read_proposal(_answer(_metadata("selected", original_name="selected")))
    wrong = context.read_proposal(_answer(_metadata("other", original_name="other")))
    assert context.validate_replacement(matching) is None
    assert context.validate_replacement(wrong) is not None


@pytest.mark.parametrize("existing", [False, True])
def test_workspace_requires_explicit_save_even_with_auto_accept(existing, monkeypatch):
    metadata = _metadata(original_name="events_by_kind" if existing else None)
    _install(monkeypatch, lambda request: httpx.Response(
        200, headers={"Content-Type": "text/event-stream"},
        content=_frame(_answer(metadata)) + _frame(done=True) + b"data: [DONE]\n\n"))

    async def scenario():
        session = _session()
        session.update_settings(ai_auto_accept=True)
        original = session.save_fn("events_by_kind", "", OLD_BODY, "Original") if existing else None
        app = D8RApp(session)
        async with app.run_test(size=(160, 62)) as pilot:
            app.action_ai()
            panel = app.ai_panel
            query = app.editor.text
            panel.query_one("#ai-input", TextArea).load_text("Edit events_by_kind" if existing else "Create events_by_kind")
            panel.action_send()
            await _finished(panel)
            assert session.fns.get("events_by_kind") == original
            assert not panel.manager.apply(panel.chat, panel, automatic=True)
            assert app.editor.text == query
            save = panel.query_one("#ai-apply", Button)
            assert str(save.label) == "Save function"
            assert not save.disabled
            await pilot.click(save)
            await pilot.pause()
            assert session.fns["events_by_kind"].body == BODY
            assert session.fns["events_by_kind"].params == ("kind",)
            assert Session().fns["events_by_kind"] == session.fns["events_by_kind"]
            assert app.editor.text == query
            assert panel.snapshot().parameters is None
            assert app.ai_panel is panel and panel.display
            assert panel.chat.proposal is None
            assert not session.history

    asyncio.run(scenario())


@pytest.mark.parametrize("when", ["streaming", "complete"])
@pytest.mark.parametrize("mutation", ["changed", "deleted", "collision"])
def test_workspace_rejects_saved_definition_changes_before_explicit_save(when, mutation, monkeypatch):
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()
        metadata = _metadata(original_name=None if mutation == "collision" else "events_by_kind")

        class Pending(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield _frame(_answer(metadata))
                started.set()
                await release.wait()
                yield _frame(done=True) + b"data: [DONE]\n\n"

        _install(monkeypatch, lambda request: httpx.Response(
            200, headers={"Content-Type": "text/event-stream"}, stream=Pending()))
        session = _session()
        if mutation != "collision":
            session.save_fn("events_by_kind", "", OLD_BODY, "Original")
        app = D8RApp(session)
        async with app.run_test(size=(160, 62)) as pilot:
            app.action_ai()
            panel = app.ai_panel
            query = app.editor.text
            panel.query_one("#ai-input", TextArea).load_text("Propose the function")
            panel.action_send()
            await asyncio.wait_for(started.wait(), 5)
            assert panel.query_one("#ai-apply", Button).disabled
            if when == "complete":
                release.set()
                await _finished(panel)
            if mutation == "deleted":
                session.delete_fn("events_by_kind")
            else:
                session.save_fn("events_by_kind", "", OLD_BODY, "Newer human definition")
            definitions = dict(session.fns)
            stored = session.storage_path.read_bytes()
            release.set()
            await _finished(panel)
            panel.action_apply()
            await pilot.pause()
            assert session.fns == definitions
            assert session.storage_path.read_bytes() == stored
            assert app.editor.text == query
            assert not session.history
            assert panel.query_one("#ai-apply", Button).disabled
            assert panel.messages[-1]["content"] == _answer(metadata)

    asyncio.run(scenario())


def test_save_failure_preserves_proposal_for_retry_without_changing_query(monkeypatch):
    _install(monkeypatch, lambda request: httpx.Response(
        200, headers={"Content-Type": "text/event-stream"},
        content=_frame(_answer(_metadata())) + _frame(done=True) + b"data: [DONE]\n\n"))

    async def scenario():
        session = _session()
        save_fn = session.save_fn

        def unavailable(*args, **kwargs):
            raise OSError("private storage details")

        app = D8RApp(session)
        async with app.run_test(size=(160, 62)) as pilot:
            app.action_ai()
            panel = app.ai_panel
            query = app.editor.text
            panel.query_one("#ai-input", TextArea).load_text("Create a reusable function")
            panel.action_send()
            await _finished(panel)
            proposal = panel.chat.proposal
            monkeypatch.setattr(session, "save_fn", unavailable)
            save = panel.query_one("#ai-apply", Button)
            await pilot.click(save)
            await pilot.pause(save.active_effect_duration)
            assert panel.chat.proposal is proposal
            assert not save.disabled
            assert not session.fns
            assert app.editor.text == query
            assert "private storage details" not in panel.chat.diagnostics.text
            monkeypatch.setattr(session, "save_fn", save_fn)
            await pilot.click(save)
            await pilot.pause()
            assert session.fns["events_by_kind"].body == BODY
            assert panel.chat.proposal is None
            assert app.editor.text == query
            assert not session.history

    asyncio.run(scenario())
