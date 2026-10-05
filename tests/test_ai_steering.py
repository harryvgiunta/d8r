"""Steering interrupts only its generation and replays only safe conversation state."""
from __future__ import annotations

import asyncio
import json
import threading

import httpx
import pytest
from textual.widgets import Button, Static, TextArea

from d8r.ai import client, context as context_module
from d8r.ai.client import AIConfig
from d8r.tui.app import D8RApp
from d8r.tui.session import Session


BODY = "\\from events\n\\limit 2"
ANSWER = "A complete draft.\n```d8r\n" + BODY + "\n```"


def _frame(delta, finish=None):
    return ("data: " + json.dumps({"choices": [{"index": 0, "delta": delta,
                                               "finish_reason": finish}]}) + "\n\n").encode()


def _answer(text=ANSWER):
    return _frame({"content": text}) + _frame({}, "stop") + b"data: [DONE]\n\n"


def _call(name, arguments, call_id, index=0):
    return {"index": index, "id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}


def _tools(*calls):
    return _frame({"tool_calls": list(calls)}) + _frame({}, "tool_calls") + b"data: [DONE]\n\n"


def _save(name):
    return {"name": name, "original_name": None, "description": "Two events",
            "parameters": [], "body": BODY}


def _install(monkeypatch, provider):
    def handle(request):
        response = provider(json.loads(request.content))
        response.headers["Content-Type"] = "text/event-stream"
        return response

    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(client, "create_client", lambda config: httpx.AsyncClient(transport=transport))


def _session(path):
    session = Session(data_dir=path)
    session.ai_config = AIConfig("https://provider.invalid/v1", "test", "private-key", max_attempts=1)
    return session


def _send(panel, prompt):
    panel.query_one("#ai-input", TextArea).load_text(prompt)
    panel.action_send()


async def _finished(chat):
    async def wait():
        while chat.worker is not None:
            await asyncio.sleep(0.01)
    await asyncio.wait_for(wait(), 8)


def test_enter_steers_same_chat_with_original_request_and_complete_tool_history(tmp_path, monkeypatch):
    async def scenario():
        started = asyncio.Event()
        closed = asyncio.Event()
        requests = []

        class Partial(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield _frame({"content": ANSWER})
                yield _frame({"tool_calls": [_call("save_function", _save("never_saved"), "incomplete-save")]})
                started.set()
                await asyncio.Event().wait()

            async def aclose(self):
                closed.set()

        def provider(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(200, content=_answer("Earlier completed discussion."))
            if len(requests) == 2:
                return httpx.Response(200, content=_tools(_call("schema", {"table": "events"}, "schema-1")))
            if len(requests) == 3:
                return httpx.Response(200, stream=Partial())
            return httpx.Response(200, content=_answer("No changes requested now."))

        _install(monkeypatch, provider)
        session = _session(tmp_path)
        session.update_settings(ai_auto_accept=True)
        app = D8RApp(session)
        async with app.run_test(size=(150, 54)) as pilot:
            app.action_ai()
            panel = app.ai_panel
            original = app.editor.text
            _send(panel, "Earlier question")
            await _finished(panel.chat)
            _send(panel, "Inspect events and make a draft")
            chat = panel.chat
            await asyncio.wait_for(started.wait(), 5)
            composer = panel.query_one("#ai-input", TextArea)
            assert not composer.disabled and not composer.read_only
            assert not panel.query_one("#ai-send", Button).disabled
            assert panel.query_one("#ai-apply", Button).disabled
            composer.load_text("Instead explain it; do not save or apply anything")
            composer.focus()
            await pilot.press("enter")
            await asyncio.wait_for(closed.wait(), 5)
            await _finished(chat)
            assert panel.chat is chat
            sent = requests[-1]["messages"]
            assert [message["role"] for message in sent] == [
                "system", "user", "assistant", "user", "assistant", "tool", "assistant", "user",
            ]
            assert [message["content"] for message in sent if message["role"] == "user"] == [
                "Earlier question", "Inspect events and make a draft",
                "Instead explain it; do not save or apply anything",
            ]
            assert sent[2]["content"] == "Earlier completed discussion."
            schema_result = next(message for message in sent if message["role"] == "tool")
            assert json.loads(schema_result["content"])["tables"][0]["name"] == "events"
            assert "incomplete-save" not in json.dumps(sent)
            assert ANSWER not in [message.get("content") for message in sent]
            assert "Inspect events and make a draft" in str(panel.query_one("#ai-transcript", Static).content)
            assert app.editor.text == original
            assert not session.fns and not session.history
            assert panel.query_one("#ai-apply", Button).disabled

    asyncio.run(scenario())


def test_rapid_steering_waits_for_read_and_preserves_committed_save_without_partial_batch(tmp_path, monkeypatch):
    async def scenario():
        started = threading.Event()
        release = threading.Event()
        execute = context_module.execute
        requests = []
        active_reads = 0
        max_reads = 0
        reads = 0

        def delayed_execute(*args, **kwargs):
            nonlocal active_reads, max_reads, reads
            active_reads += 1
            max_reads = max(max_reads, active_reads)
            reads += 1
            try:
                if reads == 1:
                    started.set()
                    if not release.wait(10):
                        raise RuntimeError("test did not release sample")
                return execute(*args, **kwargs)
            finally:
                active_reads -= 1

        def provider(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(200, content=_tools(_call("schema", {"table": "events"}, "safe-schema")))
            if len(requests) == 2:
                return httpx.Response(200, content=_tools(
                    _call("save_function", _save("already_saved"), "saved-before-interrupt"),
                    _call("sample_rows", {"table": "events", "limit": 2}, "unfinished-sample", 1),
                    _call("save_function", _save("never_saved"), "abandoned-save", 2),
                ))
            if len(requests) == 3:
                return httpx.Response(200, content=_tools(
                    _call("sample_rows", {"table": "events", "limit": 1}, "fresh-sample")))
            return httpx.Response(200, content=_answer("Completed the newest instruction."))

        monkeypatch.setattr(context_module, "execute", delayed_execute)
        _install(monkeypatch, provider)
        session = _session(tmp_path)
        session.update_settings(ai_allow_sample_data=True)
        app = D8RApp(session)
        async with app.run_test(size=(150, 54)) as pilot:
            app.action_ai()
            panel = app.ai_panel
            original = app.editor.text
            _send(panel, "Save already_saved, inspect a sample, then save never_saved")
            chat = panel.chat
            try:
                assert await asyncio.to_thread(started.wait, 5)
                _send(panel, "Do not save anything else; explain the sample")
                _send(panel, "Keep the explanation brief")
                assert panel.busy and panel.query_one("#ai-input", TextArea).text == ""
                assert session.busy and not panel.query_one("#ai-send", Button).disabled
                await pilot.pause()
                assert len(requests) == 2  # Both new generations must wait for the old connection read.
                panel.action_clear()
                other = panel.chat
                panel.query_one("#ai-input", TextArea).load_text("Unrelated unsent message")
                release.set()
                await _finished(chat)
                assert panel.chat is other
                assert panel.query_one("#ai-input", TextArea).text == "Unrelated unsent message"
                assert chat.unread
                assert reads == 2 and max_reads == 1
                sent = requests[2]["messages"]
                assert [message["content"] for message in sent if message["role"] == "user"] == [
                    "Save already_saved, inspect a sample, then save never_saved",
                    "Do not save anything else; explain the sample", "Keep the explanation brief",
                ]
                tool_ids = [message["tool_call_id"] for message in sent if message["role"] == "tool"]
                assert tool_ids == ["safe-schema"]
                assert not any(message.get("tool_calls", [{}])[0].get("id") == "saved-before-interrupt"
                               for message in sent)
                assert any(message["role"] == "assistant" and "already_saved" in (message.get("content") or "")
                           for message in sent)
                assert set(session.fns) == {"already_saved"}
                assert set(Session(data_dir=tmp_path).fns) == {"already_saved"}
                panel.select_chat(chat.id)
                assert "already_saved" in str(panel.query_one("#ai-transcript", Static).content)
                assert app.editor.text == original and not session.history
            finally:
                release.set()
                await asyncio.gather(*app.ai_chats.context_tasks)

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [client.AIError, RuntimeError], ids=["provider-error", "internal-error"])
def test_late_cancelled_worker_failure_cannot_clobber_new_response(tmp_path, monkeypatch, failure):
    async def scenario():
        started = asyncio.Event()
        cancelled = asyncio.Event()
        release_old = asyncio.Event()
        old_done = asyncio.Event()
        new_started = asyncio.Event()
        release_new = asyncio.Event()
        requests = []

        class Old(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield _frame({"content": "Abandoned partial answer"})
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    await release_old.wait()
                old_done.set()
                raise failure("Obsolete failure")

        class New(httpx.AsyncByteStream):
            async def __aiter__(self):
                new_started.set()
                await release_new.wait()
                yield _answer()

        def provider(request):
            requests.append(request)
            return httpx.Response(200, stream=Old() if len(requests) == 1 else New())

        _install(monkeypatch, provider)
        app = D8RApp(_session(tmp_path))
        async with app.run_test(size=(140, 48)) as pilot:
            app.action_ai()
            panel = app.ai_panel
            _send(panel, "Original request")
            await asyncio.wait_for(started.wait(), 5)
            _send(panel, "Make the replacement two rows")
            await asyncio.wait_for(cancelled.wait(), 5)
            await asyncio.wait_for(new_started.wait(), 5)
            release_old.set()
            await asyncio.wait_for(old_done.wait(), 5)
            await pilot.pause()
            assert panel.busy and panel.chat.status == "working"
            assert not panel.query_one("#ai-cancel", Button).disabled
            assert "Obsolete failure" not in str(panel.query_one("#ai-status", Static).content)
            release_new.set()
            await _finished(panel.chat)
            assert not panel.query_one("#ai-apply", Button).disabled
            panel.action_apply()
            assert app.editor.text == BODY
            assert not app.session.history and not app.session.fns

    asyncio.run(scenario())


@pytest.mark.parametrize("interruption", ["cancel", "shutdown"])
def test_cancel_or_shutdown_discards_queued_steering_without_losing_its_draft(tmp_path, monkeypatch, interruption):
    async def scenario():
        started = threading.Event()
        release = threading.Event()
        execute = context_module.execute
        requests = []

        def delayed_execute(*args, **kwargs):
            started.set()
            if not release.wait(10):
                raise RuntimeError("test did not release sample")
            return execute(*args, **kwargs)

        def provider(request):
            requests.append(request)
            return httpx.Response(200, content=_tools(
                _call("sample_rows", {"table": "events", "limit": 1}, "sample"),
                _call("save_function", _save("never_saved"), "abandoned-save", 1),
            ))

        monkeypatch.setattr(context_module, "execute", delayed_execute)
        _install(monkeypatch, provider)
        app = D8RApp(_session(tmp_path))
        app.session.update_settings(ai_allow_sample_data=True)
        chat = None
        try:
            async with app.run_test(size=(140, 48)) as pilot:
                app.action_ai()
                panel = app.ai_panel
                _send(panel, "Read a sample, then save never_saved")
                chat = panel.chat
                assert await asyncio.to_thread(started.wait, 5)
                _send(panel, "Do not save; explain only")
                await pilot.pause()  # Let the replacement actually enter its shielded wait.
                if interruption == "cancel":
                    panel.action_cancel()
                    assert panel.query_one("#ai-input", TextArea).text == "Do not save; explain only"
                    release.set()
                    await asyncio.gather(*app.ai_chats.context_tasks)
                    await pilot.pause()
                else:
                    app.exit()
            release.set()
            await asyncio.gather(*app.ai_chats.context_tasks)
        finally:
            release.set()
        assert len(requests) == 1
        assert chat.status == "cancelled" and chat.proposal is None
        assert not app.session.fns and not app.session.history
        restored = D8RApp(_session(tmp_path))
        async with restored.run_test(size=(140, 48)):
            restored.ai_panel.select_chat(chat.id)
            panel = restored.ai_panel
            assert panel.query_one("#ai-input", TextArea).text == "Do not save; explain only"
            assert [message["content"] for message in panel.messages if message["role"] == "user"] == [
                "Read a sample, then save never_saved",
            ]
            assert not panel.busy and panel.query_one("#ai-apply", Button).disabled
            assert len(requests) == 1

    asyncio.run(scenario())
