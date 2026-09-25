"""Durable AI conversations never restore executable proposals or unfinished turns."""
from __future__ import annotations

import asyncio
import json

import httpx
from textual.app import App, ComposeResult
from textual.widgets import Button, Static, TextArea

from d8r.ai import client
from d8r.ai.client import AIConfig
from d8r.ai.context import AIContext
from d8r.tui.ai import AIPanel, AITarget
from d8r.tui.session import HistoryEntry, Session


ANSWER = "Review this draft.\n```d8r\n\\from events\n\\limit 2\n```"


def _frame(delta: dict, finish=None) -> bytes:
    return ("data: " + json.dumps({"choices": [{"index": 0, "delta": delta,
                                               "finish_reason": finish}]}) + "\n\n").encode()


def _answer(text=ANSWER) -> bytes:
    return _frame({"content": text}) + _frame({}, "stop") + b"data: [DONE]\n\n"


def _provider(monkeypatch, handler) -> None:
    def stream(request):
        response = handler(request)
        response.headers["Content-Type"] = "text/event-stream"
        return response

    transport = httpx.MockTransport(stream)
    monkeypatch.setattr(client, "create_client", lambda config: httpx.AsyncClient(transport=transport))


def _session() -> Session:
    session = Session()
    session.ai_config = AIConfig("https://provider.invalid/v1", "test", "private-key", max_attempts=1)
    return session


class ChatApp(App):
    """Real panel lifecycle, independent of the owning editor's persistence."""

    def __init__(self, session: Session, *, function_mode=False):
        super().__init__()
        self.session = session
        self.document = "\\from events"
        self.document_id = "stable-document"
        self.function_mode = function_mode
        self.applied = []

    def snapshot(self) -> AITarget:
        return AITarget((self.session.source_key(), self.document_id),
                        self.session.active_id, self.document,
                        () if self.function_mode else None)

    def compose(self) -> ComposeResult:
        yield AIPanel(self.session, self.snapshot, self.applied.append, lambda: None,
                      function_mode=self.function_mode, id="chat")

    @property
    def panel(self) -> AIPanel:
        return self.query_one(AIPanel)


async def _finished(panel: AIPanel) -> None:
    async def wait():
        while panel.busy:
            await asyncio.sleep(0.01)
    await asyncio.wait_for(wait(), 8)


def test_restart_continues_complete_tool_history_with_fresh_context(monkeypatch):
    requests = []

    def provider(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            call = {"index": 0, "id": "schema-call", "type": "function",
                    "function": {"name": "schema", "arguments": '{"table":"events"}'}}
            data = _frame({"tool_calls": [call]}, "tool_calls") + b"data: [DONE]\n\n"
        else:
            data = _answer()
        return httpx.Response(200, content=data)

    _provider(monkeypatch, provider)

    async def scenario():
        app = ChatApp(_session())
        async with app.run_test() as pilot:
            panel = app.panel
            panel.open()
            panel.query_one("#ai-input", TextArea).load_text("Inspect schema and draft a query")
            panel.action_send()
            await _finished(panel)
            original_messages = panel.messages.copy()
            assert [message["role"] for message in original_messages] == ["user", "assistant", "tool", "assistant"]
            assert not panel.query_one("#ai-apply", Button).disabled
            panel.query_one("#ai-input", TextArea).load_text("Continue after restart")
            await pilot.pause()  # Unmount must flush even before the debounce fires.
        restarted = ChatApp(_session())
        restarted.document = "\\from users"
        restarted.session.default_rows = 17
        async with restarted.run_test() as pilot:
            panel = restarted.panel
            assert len(requests) == 2  # Loading the panel never calls the provider.
            assert panel.display
            assert panel.messages == original_messages
            assert panel._turn == 1
            assert panel.query_one("#ai-input", TextArea).text == "Continue after restart"
            assert "Review this draft" in str(panel.query_one("#ai-transcript", Static).content)
            assert panel.query_one("#ai-apply", Button).disabled
            assert not panel.query_one("#ai-proposal").display
            panel.action_apply()
            assert not restarted.applied
            assert not restarted.session.history
            panel.action_send()
            await _finished(panel)
            sent = requests[-1]["messages"]
            assert [index for index, message in enumerate(sent) if message["role"] == "system"] == [0]
            assert sent[1:-1] == original_messages
            assert sent[-1] == {"role": "user", "content": "Continue after restart"}
            snapshot = json.loads(sent[0]["content"].split("Editor snapshot (JSON data):\n")[1])
            assert snapshot["document"] == "\\from users"
            assert snapshot["default_rows"] == 17
            assert panel._turn == 2
    asyncio.run(scenario())


def test_source_and_mode_isolation_and_durable_new_chat(monkeypatch):
    _provider(monkeypatch, lambda request: httpx.Response(200, content=_answer("Saved answer")))

    async def scenario():
        app = ChatApp(_session())
        async with app.run_test() as pilot:
            panel = app.panel
            panel.open()
            panel.query_one("#ai-input", TextArea).load_text("Demo question")
            panel.action_send()
            await _finished(panel)
            panel.query_one("#ai-input", TextArea).load_text("Demo draft")
            app.session.set_active("mysql")
            panel.target_changed()
            assert not panel.messages
            assert panel.query_one("#ai-input", TextArea).text == ""
            assert panel.display  # A new target keeps the currently open helper.
            panel.open()
            panel.query_one("#ai-input", TextArea).load_text("MySQL question")
            panel.action_send()
            await _finished(panel)
            panel.query_one("#ai-input", TextArea).load_text("MySQL draft")
            panel.action_close()
            app.session.set_active("demo")
            panel.target_changed()
            assert panel.display
            assert panel.messages[0]["content"] == "Demo question"
            assert panel.query_one("#ai-input", TextArea).text == "Demo draft"
            panel.action_clear()
            assert not panel.messages
            assert panel.query_one("#ai-input", TextArea).text == ""
            assert panel.query_one("#ai-apply", Button).disabled
        restarted = ChatApp(_session())
        async with restarted.run_test():
            panel = restarted.panel
            assert not panel.messages
            assert panel.query_one("#ai-input", TextArea).text == ""
            restarted.session.set_active("mysql")
            panel.target_changed()
            assert not panel.display
            assert panel.messages[0]["content"] == "MySQL question"
            assert panel.query_one("#ai-input", TextArea).text == "MySQL draft"
        function_app = ChatApp(_session(), function_mode=True)
        function_app.session.set_active("mysql")
        async with function_app.run_test():
            assert not function_app.panel.messages
            assert function_app.panel.query_one("#ai-input", TextArea).text == ""
    asyncio.run(scenario())


def test_cancelled_and_failed_partial_turns_are_not_replayed(monkeypatch):
    requests = []
    streaming = asyncio.Event()

    class Pending(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield _frame({"content": "unfinished secret marker"})
            streaming.set()
            await asyncio.Event().wait()

    def provider(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(200, stream=Pending())
        if len(requests) == 2:
            return httpx.Response(200, content=_frame({"content": "broken partial marker"}))
        return httpx.Response(200, content=_answer("Complete reply"))

    _provider(monkeypatch, provider)

    async def scenario():
        app = ChatApp(_session())
        async with app.run_test() as pilot:
            panel = app.panel
            panel.open()
            panel.query_one("#ai-input", TextArea).load_text("Cancelled prompt")
            panel.action_send()
            await asyncio.wait_for(streaming.wait(), 8)
            panel.action_cancel()
            panel.query_one("#ai-input", TextArea).load_text("Failed prompt")
            panel.action_send()
            await _finished(panel)
            assert not panel.messages
            assert panel.query_one("#ai-input", TextArea).text == "Failed prompt"
        restarted = ChatApp(_session())
        async with restarted.run_test():
            panel = restarted.panel
            assert not panel.messages
            assert not panel.query_one("#ai-transcript", Static).content
            assert not panel._diagnostics.text
            assert panel.query_one("#ai-apply", Button).disabled
            panel.query_one("#ai-input", TextArea).load_text("Fresh request")
            panel.action_send()
            await _finished(panel)
            assert [message["role"] for message in requests[-1]["messages"]] == ["system", "user"]
            assert requests[-1]["messages"][-1]["content"] == "Fresh request"
            assert "partial marker" not in json.dumps(requests[-1])
            assert "Cancelled prompt" not in json.dumps(requests[-1])
    asyncio.run(scenario())


def test_autosave_failure_keeps_completed_response_and_draft(monkeypatch):
    _provider(monkeypatch, lambda request: httpx.Response(200, content=_answer()))

    async def scenario():
        session = _session()
        app = ChatApp(session)
        async with app.run_test() as pilot:
            panel = app.panel
            panel.open()
            save = session._workspace.save

            def fail(*args, **kwargs):
                raise ValueError("private-key must never appear in a save error")

            monkeypatch.setattr(session._workspace, "save", fail)
            panel.query_one("#ai-input", TextArea).load_text("Generate")
            panel.action_send()
            await _finished(panel)
            assert panel.messages[-1]["content"] == ANSWER
            assert not panel.query_one("#ai-apply", Button).disabled
            panel.query_one("#ai-input", TextArea).load_text("Keep this draft")
            await pilot.pause(0.4)
            status = str(panel.query_one("#ai-status", Static).content)
            assert "could not be saved" in status
            assert "private-key" not in status
            assert panel.query_one("#ai-input", TextArea).text == "Keep this draft"
            app.session.set_active("mysql")
            panel.target_changed()
            app.session.set_active("demo")
            panel.target_changed()
            assert panel.messages[-1]["content"] == ANSWER
            assert panel.query_one("#ai-input", TextArea).text == "Keep this draft"
            monkeypatch.setattr(session._workspace, "save", save)
            panel.action_close()
        restarted = ChatApp(_session())
        async with restarted.run_test():
            assert restarted.panel.messages[-1]["content"] == ANSWER
            assert restarted.panel.query_one("#ai-input", TextArea).text == "Keep this draft"
    asyncio.run(scenario())


def test_history_context_uses_canonical_identity_and_newest_first():
    async def scenario():
        session = _session()
        key = session.source_key("demo")
        session.history = [
            HistoryEntry("new", "previous-slug", "duckdb", 2, 1, "newest", target=key),
            HistoryEntry("other", "demo", "duckdb", 3, 1, "wrong source", target="mysql"),
            HistoryEntry("old", "demo", "duckdb", 1, 1, "oldest", target=key),
        ]
        context = AIContext(session, "demo", "")
        result = json.loads(await context.call_tool("query_history", {}))
        assert [entry["document"] for entry in result["history"]] == ["newest", "oldest"]
    asyncio.run(scenario())


def test_disconnected_target_keeps_composer_without_sending(monkeypatch):
    requests = []

    def provider(request):
        requests.append(request)
        return httpx.Response(200, content=_answer())

    _provider(monkeypatch, provider)

    async def scenario():
        session = _session()
        session.restore_source("saved-d1:account:11111111-2222-3333-4444-555555555555")
        app = ChatApp(session)
        async with app.run_test():
            panel = app.panel
            panel.open()
            panel.query_one("#ai-input", TextArea).load_text("Keep until reconnected")
            panel.action_send()
            assert not requests
            assert not panel.busy
            assert not panel.messages
            assert panel.query_one("#ai-input", TextArea).text == "Keep until reconnected"
            assert "disconnected" in str(panel.query_one("#ai-status", Static).content)
    asyncio.run(scenario())


def test_immediate_exit_flushes_composer_before_changed_event():
    async def scenario():
        app = ChatApp(_session())
        draft = "Continue after restart\nKeep the entire final line"
        async with app.run_test():
            panel = app.panel
            panel.open()
            panel.query_one("#ai-input", TextArea).load_text(draft)
            app.exit()  # No pause: Changed and the debounce have not run yet.
        restarted = ChatApp(_session())
        async with restarted.run_test():
            assert restarted.panel.display
            assert restarted.panel.query_one("#ai-input", TextArea).text == draft
            assert not restarted.panel.messages
            assert restarted.panel.query_one("#ai-apply", Button).disabled
    asyncio.run(scenario())
