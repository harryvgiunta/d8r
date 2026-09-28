"""Independent, durable AI agents exercised through real Textual editor views."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from textual.widgets import Button, OptionList, Static, TextArea

from d8r.ai import client
from d8r.ai.client import AIConfig
from d8r.tui.ai import AgentsScreen
from d8r.tui.app import D8RApp
from d8r.tui.session import Session


BODY = "\\from events\n\\limit 2"
ANSWER = "A complete draft.\n```d8r\n" + BODY + "\n```"


def _frame(text: str = "", *, done: bool = False) -> bytes:
    delta = {} if done else {"content": text}
    finish = "stop" if done else None
    return ("data: " + json.dumps({"choices": [{"index": 0, "delta": delta,
                                               "finish_reason": finish}]}) + "\n\n").encode()


def _install(monkeypatch, handler) -> None:
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(client, "create_client", lambda config: httpx.AsyncClient(transport=transport))


def _session(path) -> Session:
    session = Session(data_dir=path)
    session.ai_config = AIConfig("https://provider.invalid/v1", "test", "private-key", max_attempts=1)
    return session


async def _finished(chat) -> None:
    async def wait():
        while chat.worker is not None:
            await asyncio.sleep(0.01)
    await asyncio.wait_for(wait(), 8)


def test_parallel_same_target_switching_preserves_drafts_and_marks_unread(tmp_path, monkeypatch):
    async def scenario():
        gates = {name: asyncio.Event() for name in ("first", "second")}
        started = {name: asyncio.Event() for name in gates}
        received = {}

        class Pending(httpx.AsyncByteStream):
            def __init__(self, name):
                self.name = name

            async def __aiter__(self):
                yield _frame("Reply to " + self.name)
                started[self.name].set()
                await gates[self.name].wait()
                yield _frame(done=True) + b"data: [DONE]\n\n"

        def provider(request):
            messages = json.loads(request.content)["messages"]
            name = messages[-1]["content"]
            received[name] = messages
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=Pending(name))

        _install(monkeypatch, provider)
        app = D8RApp(_session(tmp_path))
        async with app.run_test(size=(140, 48)) as pilot:
            app.action_ai()
            panel = app.ai_panel
            panel.query_one("#ai-input", TextArea).load_text("first")
            panel.action_send()
            first = panel.chat
            await asyncio.wait_for(started["first"].wait(), 5)
            panel.query_one("#ai-input", TextArea).load_text("First composer draft")
            panel.action_clear()
            second = panel.chat
            assert second.id != first.id
            panel.query_one("#ai-input", TextArea).load_text("second")
            panel.action_send()
            await asyncio.wait_for(started["second"].wait(), 5)
            assert first.status == second.status == "working"
            assert first.worker is not None and second.worker is not None
            assert all([message["role"] for message in messages] == ["system", "user"]
                       for messages in received.values())
            panel.query_one("#ai-input", TextArea).load_text("Second composer draft")
            panel.select_chat(first.id)
            assert panel.query_one("#ai-input", TextArea).text == "First composer draft"
            gates["second"].set()
            await _finished(second)
            assert second.label == "awaiting read"
            assert "Reply to second" not in str(panel.query_one("#ai-transcript", Static).content)
            app.action_agents()
            await pilot.pause()
            assert isinstance(app.screen, AgentsScreen)
            options = app.screen.query_one(OptionList)
            options.highlighted = options.get_option_index(second.id)
            options.focus()
            await pilot.press("enter")
            await pilot.pause()
            assert panel.chat_id == second.id
            assert not second.unread
            assert panel.query_one("#ai-input", TextArea).text == "Second composer draft"
            assert "Reply to second" in str(panel.query_one("#ai-transcript", Static).content)
            panel.action_close()
            assert first.worker is not None  # Hiding another view never cancels this request.
            gates["first"].set()
            await _finished(first)
            assert first.label == "awaiting read"
            panel.select_chat(first.id)
            assert "Reply to first" in str(panel.query_one("#ai-transcript", Static).content)
            assert not first.unread
            assert not app.session.history

    asyncio.run(scenario())


def test_all_chats_restore_without_network_or_applicable_proposals(tmp_path, monkeypatch):
    calls = []

    def provider(request):
        calls.append(request)
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                              content=_frame(ANSWER) + _frame(done=True) + b"data: [DONE]\n\n")

    _install(monkeypatch, provider)

    async def scenario():
        app = D8RApp(_session(tmp_path))
        async with app.run_test(size=(130, 44)):
            app.action_ai()
            panel = app.ai_panel
            panel.query_one("#ai-input", TextArea).load_text("Keep this conversation")
            panel.action_send()
            first = panel.chat
            await _finished(first)
            panel.query_one("#ai-input", TextArea).load_text("Unsent follow-up")
            panel.action_clear()
            second_id = panel.chat_id
            panel.query_one("#ai-input", TextArea).load_text("Independent draft")
            app.exit()
        restored = D8RApp(_session(tmp_path))
        async with restored.run_test(size=(130, 44)):
            panel = restored.ai_panel
            assert len(calls) == 1
            assert panel.chat_id == second_id
            assert panel.query_one("#ai-input", TextArea).text == "Independent draft"
            panel.select_chat(first.id)
            assert panel.messages[-1]["content"] == ANSWER
            assert panel.query_one("#ai-input", TextArea).text == "Unsent follow-up"
            assert ANSWER in str(panel.query_one("#ai-transcript", Static).content)
            assert panel.query_one("#ai-apply", Button).disabled
            assert not panel.query_one("#ai-proposal").display
            assert all(chat.worker is None for chat in restored.ai_chats.chats.values())
            original = restored.editor.text
            panel.action_apply()
            assert restored.editor.text == original
            assert not restored.session.history

    asyncio.run(scenario())


def test_legacy_target_chat_migrates_into_selectable_history(tmp_path):
    initial = _session(tmp_path)
    key = json.dumps(["workspace", initial.source_key(), "old-document"])
    document = dict(initial.workspace)
    document["document_id"] = "old-document"
    document["chats"] = {key: {
        "messages": [{"role": "user", "content": "An old question"},
                     {"role": "assistant", "content": "An old answer"}],
        "transcript": "You: An old question\n\nAssistant: An old answer\n\n",
        "input": "Old unfinished composer", "turns": 1, "visible": True,
    }}
    initial.workspace_path.parent.mkdir(parents=True, exist_ok=True)
    initial.workspace_path.write_text(json.dumps(document), encoding="utf-8")

    async def scenario():
        app = D8RApp(_session(tmp_path))
        async with app.run_test(size=(120, 40)):
            panel = app.ai_panel
            assert panel.messages[-1]["content"] == "An old answer"
            assert panel.query_one("#ai-input", TextArea).text == "Old unfinished composer"
            old_id = panel.chat_id
            panel.action_clear()
            assert panel.chat_id != old_id
            panel.select_chat(old_id)
            assert panel.messages[0]["content"] == "An old question"
            assert not panel.busy
            assert panel.query_one("#ai-apply", Button).disabled

    asyncio.run(scenario())


def test_function_request_survives_close_and_error_agent_can_reopen(tmp_path, monkeypatch):
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        class Pending(httpx.AsyncByteStream):
            async def __aiter__(self):
                started.set()
                await release.wait()
                yield _frame("Detached answer") + _frame(done=True) + b"data: [DONE]\n\n"

        requests = []

        def provider(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=Pending())
            return httpx.Response(503, content=b"not exposed")

        _install(monkeypatch, provider)
        app = D8RApp(_session(tmp_path))
        async with app.run_test(size=(140, 48)) as pilot:
            app.action_fn()
            await pilot.pause()
            screen = app.function_editor
            screen.ai_panel.open()
            screen.ai_panel.query_one("#ai-input", TextArea).load_text("Work in the background")
            screen.ai_panel.action_send()
            chat = screen.ai_panel.chat
            await asyncio.wait_for(started.wait(), 5)
            screen.action_back()
            await pilot.pause()
            assert not app.function_mode
            assert chat.status == "working"
            release.set()
            await _finished(chat)
            assert chat.label == "awaiting read"
            app.open_ai_chat(chat.id)
            await pilot.pause()
            assert app.function_mode
            panel = app.ai_panel
            assert panel.chat_id == chat.id
            assert "Detached answer" in str(panel.query_one("#ai-transcript", Static).content)
            panel.query_one("#ai-input", TextArea).load_text("This one fails")
            panel.action_send()
            await _finished(chat)
            assert chat.status == "error"
            panel.action_close()
            app.action_agents()
            await pilot.pause()
            options = app.screen.query_one(OptionList)
            options.highlighted = options.get_option_index(chat.id)
            options.focus()
            await pilot.press("enter")
            await pilot.pause()
            assert app.ai_panel.chat_id == chat.id
            assert "503" in str(app.ai_panel.query_one("#ai-status", Static).content)
            assert app.ai_panel.query_one("#ai-input", TextArea).text == "This one fails"
            assert not app.session.fns

    asyncio.run(scenario())


@pytest.mark.parametrize("unsafe", ["stale", "source", "invalid", "partial"])
def test_auto_accept_rejects_unsafe_response_without_losing_completed_history(tmp_path, monkeypatch, unsafe):
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()
        answer = "```d8r\n\\from events\n\\bogus\n```" if unsafe == "invalid" else ANSWER

        class Pending(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield _frame(answer)
                started.set()
                await release.wait()
                if unsafe != "partial":
                    yield _frame(done=True) + b"data: [DONE]\n\n"

        _install(monkeypatch, lambda request: httpx.Response(
            200, headers={"Content-Type": "text/event-stream"}, stream=Pending()))
        session = _session(tmp_path)
        session.update_settings(ai_auto_accept=True)
        app = D8RApp(session)
        async with app.run_test(size=(140, 48)) as pilot:
            app.action_ai()
            panel = app.ai_panel
            original = app.editor.text
            panel.query_one("#ai-input", TextArea).load_text("Propose a replacement")
            panel.action_send()
            chat = panel.chat
            await asyncio.wait_for(started.wait(), 5)
            assert app.editor.text == original  # A fenced partial reply is never applicable.
            if unsafe == "stale":
                app.editor.load_text("\\from users")
                original = app.editor.text
            elif unsafe == "source":
                app.select_source("mysql")
            await pilot.pause()
            release.set()
            await _finished(chat)
            assert app.editor.text == original
            assert not session.history and not session.fns
            if unsafe == "partial":
                assert not chat.messages
                assert chat.proposal is None
            else:
                panel.select_chat(chat.id)
                assert chat.messages[-1]["content"] == answer
                assert answer in str(panel.query_one("#ai-transcript", Static).content)
                assert panel.query_one("#ai-apply", Button).disabled
                panel.action_apply()
                assert app.editor.text == original

    asyncio.run(scenario())


def test_auto_accept_applies_only_complete_matching_proposal_without_running(tmp_path, monkeypatch):
    _install(monkeypatch, lambda request: httpx.Response(
        200, headers={"Content-Type": "text/event-stream"},
        content=_frame(ANSWER) + _frame(done=True) + b"data: [DONE]\n\n"))

    async def scenario():
        session = _session(tmp_path)
        session.update_settings(ai_auto_accept=True)
        app = D8RApp(session)
        async with app.run_test(size=(130, 44)):
            app.action_ai()
            panel = app.ai_panel
            panel.query_one("#ai-input", TextArea).load_text("Draft a query")
            panel.action_send()
            await _finished(panel.chat)
            assert app.editor.text == BODY
            assert not session.history and not session.fns
            assert panel.query_one("#ai-apply", Button).disabled

    asyncio.run(scenario())


def test_app_exit_cancels_all_agents_and_restart_does_not_resume(tmp_path, monkeypatch):
    async def scenario():
        started = [asyncio.Event(), asyncio.Event()]
        closed = [asyncio.Event(), asyncio.Event()]
        requests = []

        class Pending(httpx.AsyncByteStream):
            def __init__(self, index):
                self.index = index

            async def __aiter__(self):
                started[self.index].set()
                yield _frame("Unfinished reply")
                await asyncio.Event().wait()

            async def aclose(self):
                closed[self.index].set()

        def provider(request):
            index = len(requests)
            requests.append(request)
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=Pending(index))

        _install(monkeypatch, provider)
        app = D8RApp(_session(tmp_path))
        async with app.run_test(size=(130, 44)):
            app.action_ai()
            panel = app.ai_panel
            for index in range(2):
                if index:
                    panel.action_clear()
                panel.query_one("#ai-input", TextArea).load_text(f"Pending {index}")
                panel.action_send()
                await asyncio.wait_for(started[index].wait(), 5)
            ids = list(app.ai_chats.chats)
            app.exit()
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in closed)), 5)
        restored = D8RApp(_session(tmp_path))
        async with restored.run_test(size=(130, 44)):
            assert len(requests) == 2
            for index, chat_id in enumerate(ids):
                restored.ai_panel.select_chat(chat_id)
                assert restored.ai_panel.chat.status == "cancelled"
                assert not restored.ai_panel.busy
                assert not restored.ai_panel.messages
                assert restored.ai_panel.query_one("#ai-input", TextArea).text == f"Pending {index}"
                assert restored.ai_panel.query_one("#ai-apply", Button).disabled

    asyncio.run(scenario())


def test_function_auto_accept_only_fills_unsaved_draft(tmp_path, monkeypatch):
    metadata = {"kind": "function", "original_name": None, "name": "generated", "description": "Two events", "parameters": [], "arguments": ""}
    answer = ANSWER + "\n```json\n" + json.dumps(metadata) + "\n```"
    _install(monkeypatch, lambda request: httpx.Response(
        200, headers={"Content-Type": "text/event-stream"},
        content=_frame(answer) + _frame(done=True) + b"data: [DONE]\n\n"))

    async def scenario():
        session = _session(tmp_path)
        session.update_settings(ai_auto_accept=True)
        app = D8RApp(session)
        async with app.run_test(size=(140, 48)) as pilot:
            app.action_fn()
            await pilot.pause()
            screen = app.function_editor
            screen.ai_panel.open()
            screen.ai_panel.query_one("#ai-input", TextArea).load_text("Make a function")
            screen.ai_panel.action_send()
            await _finished(screen.ai_panel.chat)
            assert screen.query_one("#fn-body", TextArea).text == BODY
            assert screen.query_one("#fn-name").value == "generated"
            assert not session.fns
            assert not session.history

    asyncio.run(scenario())
