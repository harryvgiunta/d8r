"""Direct AI function saves are durable mutations, not executable query proposals."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from textual.widgets import Button, Static, TextArea

from d8r.ai import client
from d8r.ai.client import AIConfig
from d8r.ai.context import AIContext
from d8r.tui.app import D8RApp
from d8r.tui.session import Session


NAME = "recent_events"
BODY = "\\from events\n\\where event_type = @kind\n\\limit @n"
OLD_BODY = "\\from events\n\\limit @n"


def _arguments(*, original_name=None, **changes):
    return {"name": NAME, "original_name": original_name,
            "description": "Recent events matching a kind", "parameters": ["kind", "n"],
            "body": BODY, **changes}


def _frame(delta, finish=None):
    return ("data: " + json.dumps({"choices": [{"index": 0, "delta": delta,
                                               "finish_reason": finish}]}) + "\n\n").encode()


def _call(arguments, *, name="save_function", call_id="save-1", index=0):
    return {"index": index, "id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}


def _tools(*calls):
    return _frame({"tool_calls": list(calls)}) + _frame({}, "tool_calls") + b"data: [DONE]\n\n"


def _answer(text="The request is complete."):
    return _frame({"content": text}) + _frame({}, "stop") + b"data: [DONE]\n\n"


def _install(monkeypatch, provider):
    def handle(request):
        response = provider(json.loads(request.content))
        response.headers["Content-Type"] = "text/event-stream"
        return response

    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(client, "create_client", lambda config: httpx.AsyncClient(transport=transport))


def _session():
    session = Session()
    session.ai_config = AIConfig("https://provider.invalid/v1", "test", "private-key", max_attempts=1)
    return session


async def _finished(chat):
    async def wait():
        while chat.worker is not None:
            await asyncio.sleep(0.01)
    await asyncio.wait_for(wait(), 8)


def _send(panel, prompt):
    panel.query_one("#ai-input", TextArea).load_text(prompt)
    panel.action_send()


def _tool_results(request):
    return {message["tool_call_id"]: json.loads(message["content"])
            for message in request["messages"] if message["role"] == "tool"}


def _assert_receipt(panel):
    # The provider never emits a save claim: this must be the IDE's receipt.
    transcript = str(panel.query_one("#ai-transcript", Static).content)
    assert NAME in transcript and "saved" in transcript.casefold()
    assert NAME in panel.chat.status_text and "saved" in panel.chat.status_text.casefold()


@pytest.mark.parametrize("existing", [False, True], ids=["create", "edit-parameterized"])
def test_main_ai_saves_without_apply_or_mode_switch_and_returns_durable_success(existing, monkeypatch):
    requests = []
    arguments = _arguments(original_name=NAME if existing else None)

    def provider(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, content=_tools(
                _call({"text": BODY, "parameters": ["kind", "n"]},
                      name="validate_d8r", call_id="validate-1"),
                _call(arguments, index=1),
            ))
        return httpx.Response(200, content=_answer())

    _install(monkeypatch, provider)

    async def scenario():
        session = _session()
        if existing:
            session.save_fn(NAME, "n", OLD_BODY, "Original")
        app = D8RApp(session)
        async with app.run_test(size=(160, 62)) as pilot:
            app.editor.load_text("\\from users\n\\limit 3")
            app.action_ai()
            panel = app.ai_panel
            query = app.editor.text
            history = list(session.history)
            _send(panel, "Edit recent_events to accept kind and n and save it" if existing else
                  "Create and save recent_events(kind, n) filtering events by kind and limiting to n")
            await _finished(panel.chat)
            await pilot.pause()
            saved = session.fns[NAME]
            assert (saved.body, saved.params, saved.doc) == (BODY, ("kind", "n"), arguments["description"])
            assert Session().fns[NAME] == saved
            assert app.editor.text == query
            assert session.history == history
            assert panel.snapshot().parameters is None
            assert app.ai_panel is panel and panel.display
            assert panel.query_one("#ai-apply", Button).disabled
            results = _tool_results(requests[-1])
            assert results["validate-1"]["valid"] is True
            assert results["save-1"] == {"saved": True, "name": NAME}
            _assert_receipt(panel)

    asyncio.run(scenario())


@pytest.mark.parametrize("changes", [
    {"body": "\\from events\n\\temp forbidden"},
    {"body": "\\from events\n\\limit @undeclared"},
    {"parameters": ["n", "n"]},
    {"parameters": ["@kind", "n"]},
    {"parameters": "kind,n"},
    {"parameters": [None]},
    {"original_name": False},
    {"name": "not a name"},
    {"description": None},
    {"body": ""},
    {"overwrite": True},
], ids=["statement", "undeclared", "duplicate-parameter", "parameter-sigil", "not-array",
        "non-string-parameter", "invalid-original", "invalid-name", "invalid-description",
        "empty-body", "extra-field"])
def test_invalid_save_arguments_never_mutate_storage(changes):
    session = Session()
    original = session.save_fn("keep", "n", OLD_BODY, "Keep this definition")
    stored = session.storage_path.read_bytes()
    context = AIContext(session, "demo", "", save_guard=lambda: None)
    result = json.loads(asyncio.run(context.call_tool("save_function", _arguments(**changes))))
    assert result["saved"] is False and result["error"]
    assert session.fns == {"keep": original}
    assert session.storage_path.read_bytes() == stored
    assert not session.history


def test_missing_required_original_name_cannot_implicitly_create():
    session = Session()
    context = AIContext(session, "demo", "", save_guard=lambda: None)
    arguments = _arguments()
    del arguments["original_name"]
    result = json.loads(asyncio.run(context.call_tool("save_function", arguments)))
    assert result["saved"] is False and result["error"]
    assert not session.fns and not session.storage_path.exists()


@pytest.mark.parametrize("boundary", ["missing-baseline", "late-baseline", "collision", "late-collision",
                                     "changed", "deleted", "rename", "source-replaced", "guard"])
def test_save_rechecks_original_definition_source_and_live_guard(boundary):
    session = Session()
    if boundary in {"collision", "changed", "deleted", "rename"}:
        session.save_fn(NAME, "n", OLD_BODY, "Original")
    context = AIContext(session, "demo", "", save_guard=lambda: "Target changed" if boundary == "guard" else None)
    arguments = _arguments(original_name=NAME if boundary in {
        "missing-baseline", "late-baseline", "changed", "deleted", "rename"} else None)
    if boundary in {"late-baseline", "late-collision", "changed"}:
        session.save_fn(NAME, "n", OLD_BODY, "Newer human definition")
    elif boundary == "deleted":
        session.delete_fn(NAME)
    elif boundary == "rename":
        arguments["name"] = "renamed"
    elif boundary == "source-replaced":
        session.sources["demo"] = session.sources["mysql"]
    before = dict(session.fns)
    stored = session.storage_path.read_bytes() if session.storage_path.exists() else None
    result = json.loads(asyncio.run(context.call_tool("save_function", arguments)))
    assert result["saved"] is False and result["error"]
    assert session.fns == before
    assert (session.storage_path.read_bytes() if session.storage_path.exists() else None) == stored
    assert not session.history


def test_live_guard_is_checked_after_validation_immediately_before_persistence(monkeypatch):
    session = Session()
    allowed = True
    validate = session.validate_fn

    def change_target_during_validation(*args, **kwargs):
        nonlocal allowed
        result = validate(*args, **kwargs)
        allowed = False
        return result

    monkeypatch.setattr(session, "validate_fn", change_target_during_validation)
    context = AIContext(session, "demo", "", save_guard=lambda: None if allowed else "Target changed")
    result = json.loads(asyncio.run(context.call_tool("save_function", _arguments())))
    assert result["saved"] is False and result["error"]
    assert not session.fns and not session.storage_path.exists()


@pytest.mark.parametrize("parameters,guard", [((), lambda: None), (None, None)],
                         ids=["function-form", "unguarded-workspace"])
def test_context_without_workspace_save_authority_cannot_save(parameters, guard):
    session = Session()
    context = AIContext(session, "demo", "", parameters=parameters, save_guard=guard)
    assert "save_function" not in {tool["function"]["name"] for tool in context.tools}
    result = json.loads(asyncio.run(context.call_tool("save_function", _arguments())))
    assert result.get("saved") is not True and result["error"]
    assert not session.fns and not session.storage_path.exists()


@pytest.mark.parametrize("change", ["query", "source"])
def test_current_target_change_before_completed_tool_call_cannot_save(change, monkeypatch):
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        class Pending(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield _frame({"tool_calls": [_call(_arguments())]})
                started.set()
                await release.wait()
                yield _frame({}, "tool_calls") + b"data: [DONE]\n\n"

        requests = []

        def provider(request):
            requests.append(request)
            return httpx.Response(200, stream=Pending()) if len(requests) == 1 else httpx.Response(200, content=_answer())

        _install(monkeypatch, provider)
        session = _session()
        app = D8RApp(session)
        async with app.run_test(size=(160, 62)) as pilot:
            app.action_ai()
            panel = app.ai_panel
            _send(panel, "Create and save recent_events")
            chat = panel.chat
            await asyncio.wait_for(started.wait(), 5)
            if change == "query":
                app.editor.load_text("\\from users\n\\limit 2")
            else:
                app.select_source("mysql")
            await pilot.pause()
            query = app.editor.text
            release.set()
            await _finished(chat)
            assert NAME not in session.fns and NAME not in Session().fns
            assert not session.history
            assert app.editor.text == query
            assert not any(result.get("saved") is True for request in requests for result in _tool_results(request).values())

    asyncio.run(scenario())


@pytest.mark.parametrize("interruption", ["truncated", "cancel"])
def test_unfinished_tool_stream_never_saves(interruption, monkeypatch):
    async def scenario():
        started = asyncio.Event()
        closed = asyncio.Event()

        class Incomplete(httpx.AsyncByteStream):
            async def __aiter__(self):
                try:
                    # Even complete JSON arguments are not authority until the stream completes.
                    yield _frame({"tool_calls": [_call(_arguments())]}, "tool_calls")
                    started.set()
                    if interruption == "cancel":
                        await asyncio.Event().wait()
                finally:
                    closed.set()

        _install(monkeypatch, lambda request: httpx.Response(200, stream=Incomplete()))
        session = _session()
        app = D8RApp(session)
        async with app.run_test(size=(160, 62)) as pilot:
            app.action_ai()
            panel = app.ai_panel
            query = app.editor.text
            _send(panel, "Create and save recent_events")
            await asyncio.wait_for(started.wait(), 5)
            if interruption == "cancel":
                panel.action_cancel()
            await _finished(panel.chat)
            await asyncio.wait_for(closed.wait(), 5)
            await pilot.pause()
            assert not session.fns and not Session().fns
            assert not session.history
            assert app.editor.text == query
            assert panel.query_one("#ai-apply", Button).disabled

    asyncio.run(scenario())


@pytest.mark.parametrize("interruption", ["failure", "cancel"])
def test_completed_save_survives_later_provider_interruption_with_visible_receipt(interruption, monkeypatch):
    async def scenario():
        following = asyncio.Event()
        closed = asyncio.Event()
        requests = []

        class PendingReply(httpx.AsyncByteStream):
            async def __aiter__(self):
                try:
                    yield _frame({"content": "Finishing the response"})
                    following.set()
                    await asyncio.Event().wait()
                finally:
                    closed.set()

        def provider(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(200, content=_tools(_call(_arguments())))
            if interruption == "failure":
                return httpx.Response(503, text="Provider unavailable")
            return httpx.Response(200, stream=PendingReply())

        _install(monkeypatch, provider)
        session = _session()
        app = D8RApp(session)
        async with app.run_test(size=(160, 62)) as pilot:
            app.action_ai()
            panel = app.ai_panel
            query = app.editor.text
            _send(panel, "Create and save recent_events")
            if interruption == "cancel":
                await asyncio.wait_for(following.wait(), 5)
                panel.action_cancel()
                await asyncio.wait_for(closed.wait(), 5)
            await _finished(panel.chat)
            await pilot.pause()
            assert session.fns[NAME].body == BODY
            assert Session().fns[NAME] == session.fns[NAME]
            assert _tool_results(requests[-1])["save-1"] == {"saved": True, "name": NAME}
            assert app.editor.text == query and not session.history
            assert panel.chat.status == ("error" if interruption == "failure" else "cancelled")
            _assert_receipt(panel)
            chat_id = panel.chat.id
        restored = D8RApp(_session())
        async with restored.run_test(size=(160, 62)):
            restored.action_ai()
            restored.ai_panel.select_chat(chat_id)
            transcript = str(restored.ai_panel.query_one("#ai-transcript", Static).content)
            assert NAME in transcript and "saved" in transcript.casefold()
            assert restored.session.fns[NAME].body == BODY

    asyncio.run(scenario())


def test_followup_apply_it_uses_current_tools_after_prior_assistant_refusal(monkeypatch):
    requests = []
    refusal = "I cannot save functions from this chat. Here is the suggested body:\n```d8r\n" + BODY + "\n```"

    def provider(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, content=_answer(refusal))
        if len(requests) == 2:
            tools = {tool["function"]["name"] for tool in request.get("tools", [])}
            if "save_function" in tools:
                return httpx.Response(200, content=_tools(_call(_arguments(original_name=NAME))))
            return httpx.Response(200, content=_answer("Still unable to save."))
        return httpx.Response(200, content=_answer())

    _install(monkeypatch, provider)

    async def scenario():
        session = _session()
        original = session.save_fn(NAME, "n", OLD_BODY, "Original")
        app = D8RApp(session)
        async with app.run_test(size=(160, 62)) as pilot:
            app.action_ai()
            panel = app.ai_panel
            query = app.editor.text
            _send(panel, "Suggest how to filter recent_events by kind")
            await _finished(panel.chat)
            assert session.fns[NAME] == original
            _send(panel, "Apply it")
            await _finished(panel.chat)
            await pilot.pause()
            assert any(message.get("content") == refusal for message in requests[1]["messages"])
            assert session.fns[NAME].body == BODY
            assert session.fns[NAME].params == ("kind", "n")
            assert Session().fns[NAME] == session.fns[NAME]
            assert app.editor.text == query and not session.history
            assert panel.snapshot().parameters is None
            _assert_receipt(panel)

    asyncio.run(scenario())
