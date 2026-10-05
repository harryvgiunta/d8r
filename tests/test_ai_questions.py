"""Human clarification resumes real AI exchanges without running query drafts."""
from __future__ import annotations

import asyncio
import json
from copy import deepcopy

import httpx
import pytest
from textual.widgets import Button, Input, OptionList, Static, TextArea

from d8r.ai import client
from d8r.ai.client import AIConfig
from d8r.tui.app import D8RApp
from d8r.tui.results import ResultsTable
from d8r.tui.session import Session


BODY = "\\from events\n\\select event_type\n\\limit 2"
USERS_BODY = "\\from users\n\\limit 2"
QUESTION = {
    "question": "Which records should the query show?",
    "options": [
        {"label": "Events", "description": "Recent activity"},
        {"label": "Users", "description": "Registered users"},
    ],
    "recommended": 0,
}


def _frame(delta, finish=None):
    return ("data: " + json.dumps({"choices": [{"index": 0, "delta": delta,
                                               "finish_reason": finish}]}) + "\n\n").encode()


def _call(name, arguments, call_id, index=0):
    return {"index": index, "id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}


def _tools(*calls):
    return _frame({"tool_calls": list(calls)}) + _frame({}, "tool_calls") + b"data: [DONE]\n\n"


def _answer(text="Done. Nothing executed."):
    return _frame({"content": text}) + _frame({}, "stop") + b"data: [DONE]\n\n"


def _apply(body=BODY, call_id="apply-after-answer"):
    return _tools(_call("apply_queries", {"queries": [{"title": "Clarified query", "body": body}]}, call_id))


def _result(request, call_id):
    return next((json.loads(message["content"]) for message in request["messages"]
                 if message.get("tool_call_id") == call_id), None)


def _install(monkeypatch, provider):
    def handle(request):
        response = provider(json.loads(request.content))
        response.headers["Content-Type"] = "text/event-stream"
        return response

    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(client, "create_client", lambda config: httpx.AsyncClient(transport=transport))


def _session(path, *, timeout=60):
    session = Session(data_dir=path)
    session.ai_config = AIConfig("https://provider.invalid/v1", "test", "private-key",
                                 max_attempts=1, timeout=timeout)
    return session


def _send(panel, prompt):
    panel.query_one("#ai-input", TextArea).load_text(prompt)
    panel.action_send()


async def _finished(chat):
    async def wait():
        while chat.worker is not None:
            await asyncio.sleep(0.01)
    await asyncio.wait_for(wait(), 8)


async def _question(panel, text=QUESTION["question"]):
    async def wait():
        while not (panel.query_one("#ai-question").display
                   and text in str(panel.query_one("#ai-question-text", Static).content)):
            await asyncio.sleep(0.01)
    await asyncio.wait_for(wait(), 5)


async def _choose(pilot, panel, index):
    options = panel.query_one("#ai-question-options", OptionList)
    options.highlighted = index
    options.scroll_visible(animate=False)
    options.focus()
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()


async def _click(pilot, widget):
    widget.scroll_visible(animate=False)
    for _ in range(50):
        await pilot.pause()
        if widget.region.offset in widget.screen.region:
            break
    await pilot.click(widget)


def test_option_resumes_original_request_after_timeout_without_blocking_context_or_executing(tmp_path, monkeypatch):
    async def scenario():
        requests = []

        def provider(request):
            requests.append(request)
            prompt = next(message["content"] for message in reversed(request["messages"])
                          if message["role"] == "user")
            if prompt == "Inspect the event schema in this separate chat":
                if _result(request, "independent-schema") is None:
                    data = _tools(_call("schema", {"table": "events"}, "independent-schema"))
                else:
                    assert _result(request, "independent-schema")["tables"][0]["name"] == "events"
                    data = _answer("The separate schema inspection finished.")
            elif (answer := _result(request, "choose-records")) is None:
                data = _tools(_call("ask_user", QUESTION, "choose-records"))
            elif _result(request, "apply-after-answer") is None:
                assert answer == {"answer": "Users"}
                data = _apply(USERS_BODY)
            else:
                assert _result(request, "apply-after-answer")["applied"] is True
                data = _answer()
            return httpx.Response(200, content=data)

        _install(monkeypatch, provider)
        session = _session(tmp_path, timeout=0.5)
        app = D8RApp(session)
        async with app.run_test(size=(160, 62)) as pilot:
            app.editor.load_text("\\from events\n\\limit 1")
            app.editor.focus()
            await pilot.press("ctrl+enter")
            await app.workers.wait_for_complete()
            table = app.query_one("#results-table", ResultsTable)
            result = (deepcopy(table.raw_columns), deepcopy(table.raw_rows), table.result_total)
            assert result[1]
            history = list(session.history)
            original = app.editor.text
            app.action_ai()
            panel = app.ai_panel
            _send(panel, "Which records can I inspect? Make the query once clarified")
            original_chat = panel.chat
            await _question(panel)
            await asyncio.sleep(session.ai_config.timeout + 0.15)
            assert panel.query_one("#ai-question").display
            assert original_chat.worker is not None
            assert not session.busy
            assert app.editor.text == original and session.history == history
            assert (table.raw_columns, table.raw_rows, table.result_total) == result

            # A completed real schema read proves that the human wait does not
            # occupy the engine's shared connection lane.
            panel.action_clear()
            _send(panel, "Inspect the event schema in this separate chat")
            other = panel.chat
            await _finished(other)
            assert other.status == "idle"
            assert original_chat.worker is not None
            assert app.editor.text == original and session.history == history
            panel.select_chat(original_chat.id)
            await _question(panel)
            options = panel.query_one("#ai-question-options", OptionList)
            options.focus()
            options.highlighted = 0
            await pilot.press("down", "enter")
            await _finished(original_chat)
            await pilot.pause()
            assert app.editor.text == USERS_BODY
            assert session.workspace["document"] == USERS_BODY
            assert session.history == history
            assert (table.raw_columns, table.raw_rows, table.result_total) == result
            assert panel.query_one("#ai-apply", Button).disabled
            assert not panel.query_one("#ai-question").display
            resumed = next(request for request in requests if _result(request, "choose-records") is not None)
            assert [message["role"] for message in resumed["messages"]] == [
                "system", "user", "assistant", "tool",
            ]
            assert resumed["messages"][2]["tool_calls"][0]["id"] == "choose-records"
            assert [message["content"] for message in resumed["messages"] if message["role"] == "user"] == [
                "Which records can I inspect? Make the query once clarified",
            ]
            assert [message["tool_call_id"] for message in resumed["messages"] if message["role"] == "tool"] == [
                "choose-records",
            ]

    asyncio.run(scenario())


def test_other_rejects_blank_input_caps_typing_and_resumes_with_trimmed_custom_answer(tmp_path, monkeypatch):
    async def scenario():
        requests = []

        def provider(request):
            requests.append(request)
            if (answer := _result(request, "custom-records")) is None:
                data = _tools(_call("ask_user", QUESTION, "custom-records"))
            elif _result(request, "apply-after-answer") is None:
                assert answer == {"answer": "Recent event types only"}
                data = _apply()
            else:
                assert _result(request, "apply-after-answer")["applied"] is True
                data = _answer()
            return httpx.Response(200, content=data)

        _install(monkeypatch, provider)
        app = D8RApp(_session(tmp_path))
        async with app.run_test(size=(160, 62)) as pilot:
            app.action_ai()
            panel = app.ai_panel
            original = app.editor.text
            _send(panel, "Make a query with the records I choose")
            chat = panel.chat
            await _question(panel)
            await _choose(pilot, panel, 2)
            custom = panel.query_one("#ai-question-other", Input)
            assert custom.display
            custom.value = "   "
            custom.focus()
            await pilot.press("enter")
            await _click(pilot, panel.query_one("#ai-question-submit", Button))
            assert len(requests) == 1 and chat.worker is not None
            assert app.editor.text == original and not app.session.history
            custom.value = "a" * 2000
            custom.focus()
            await pilot.press("end", "x")
            assert custom.value == "a" * 2000
            custom.value = "  Recent event types only  "
            custom.focus()
            await pilot.press("enter")
            await _finished(chat)
            await pilot.pause()
            assert app.editor.text == BODY
            assert not app.session.history
            assert panel.query_one("#ai-apply", Button).disabled
            assert not panel.query_one("#ai-question").display

    asyncio.run(scenario())


def test_switching_pending_chats_preserves_choice_and_custom_draft_without_crossing_answers(tmp_path, monkeypatch):
    async def scenario():
        requests = []
        first_question = {**QUESTION, "question": "First chat: which records?"}
        second_question = {
            "question": "Second chat: which explanation?",
            "options": [{"label": "Brief"}, {"label": "Detailed"}],
        }

        def provider(request):
            requests.append(request)
            prompt = next(message["content"] for message in reversed(request["messages"])
                          if message["role"] == "user")
            if prompt == "Explain the second topic":
                assert _result(request, "first-question") is None
                if (answer := _result(request, "second-question")) is None:
                    data = _tools(_call("ask_user", second_question, "second-question"))
                else:
                    assert answer == {"answer": "Detailed"}
                    data = _answer("Detailed explanation for the second chat.")
            elif (answer := _result(request, "first-question")) is None:
                data = _tools(_call("ask_user", first_question, "first-question"))
            elif _result(request, "apply-after-answer") is None:
                assert answer == {"answer": "Only event types from the first chat"}
                assert _result(request, "second-question") is None
                data = _apply()
            else:
                assert _result(request, "apply-after-answer")["applied"] is True
                data = _answer()
            return httpx.Response(200, content=data)

        _install(monkeypatch, provider)
        app = D8RApp(_session(tmp_path))
        async with app.run_test(size=(160, 62)) as pilot:
            app.action_ai()
            panel = app.ai_panel
            original = app.editor.text
            _send(panel, "Build the first query")
            first = panel.chat
            await _question(panel, first_question["question"])
            options = panel.query_one("#ai-question-options", OptionList)
            options.highlighted = 0
            options.focus()
            await pilot.press("down")
            assert options.highlighted == 1
            panel.action_clear()
            _send(panel, "Explain the second topic")
            second = panel.chat
            await _question(panel, second_question["question"])
            panel.select_chat(first.id)
            await _question(panel, first_question["question"])
            assert panel.query_one("#ai-question-options", OptionList).highlighted == 1
            await _choose(pilot, panel, 2)
            panel.query_one("#ai-question-other", Input).value = "Only event types from the first chat"
            await pilot.pause()
            panel.select_chat(second.id)
            await _question(panel, second_question["question"])
            assert panel.query_one("#ai-question-other", Input).value == ""
            await _choose(pilot, panel, 1)
            await _finished(second)
            assert first.worker is not None
            assert app.editor.text == original and not app.session.history
            assert "Detailed explanation for the second chat." in str(panel.query_one("#ai-transcript", Static).content)
            panel.select_chat(first.id)
            await _question(panel, first_question["question"])
            assert panel.query_one("#ai-question-options", OptionList).highlighted == 2
            custom = panel.query_one("#ai-question-other", Input)
            assert custom.display and custom.value == "Only event types from the first chat"
            await _click(pilot, panel.query_one("#ai-question-submit", Button))
            await _finished(first)
            await pilot.pause()
            assert app.editor.text == BODY and not app.session.history
            assert panel.query_one("#ai-apply", Button).disabled
            assert len(requests) == 5

    asyncio.run(scenario())


@pytest.mark.parametrize("interruption", ["cancel", "steer"])
def test_interruption_discards_partial_tool_batch_and_cannot_replay_old_answers(tmp_path, monkeypatch, interruption):
    async def scenario():
        requests = []
        fresh_question = {**QUESTION, "question": "Fresh clarification, not the abandoned one"}
        initial_question = {**QUESTION, "question": "Initial choice before another clarification"}

        def provider(request):
            requests.append(request)
            if len(requests) == 1:
                data = _tools(
                    _call("ask_user", initial_question, "abandoned-answer"),
                    _call("ask_user", QUESTION, "abandoned-question", 1),
                    _call("apply_queries", {"queries": [{"title": "Must never apply", "body": BODY}]},
                          "abandoned-apply", 2),
                )
            else:
                replay = json.dumps(request["messages"])
                assert "abandoned-answer" not in replay
                assert "abandoned-question" not in replay
                assert "abandoned-apply" not in replay
                assert "Abandoned custom answer" not in replay
                if (answer := _result(request, "fresh-question")) is None:
                    data = _tools(_call("ask_user", fresh_question, "fresh-question"))
                else:
                    assert answer == {"answer": "Events"}
                    data = _answer("Explanation only; no query changes.")
            return httpx.Response(200, content=data)

        _install(monkeypatch, provider)
        session = _session(tmp_path)
        session.update_settings(ai_auto_accept=True)
        app = D8RApp(session)
        async with app.run_test(size=(160, 62)) as pilot:
            app.action_ai()
            panel = app.ai_panel
            original = app.editor.text
            pages = deepcopy(session.workspace["pages"])
            _send(panel, "Make a query after clarification")
            chat = panel.chat
            await _question(panel, initial_question["question"])
            await _choose(pilot, panel, 1)
            await _question(panel)
            await _choose(pilot, panel, 2)
            panel.query_one("#ai-question-other", Input).value = "Abandoned custom answer"
            await pilot.pause()
            if interruption == "cancel":
                await _click(pilot, panel.query_one("#ai-cancel", Button))
                await _finished(chat)
                assert not panel.query_one("#ai-question").display
                assert app.editor.text == original and session.workspace["pages"] == pages
                _send(panel, "Instead explain only; ask a fresh clarification")
            else:
                composer = panel.query_one("#ai-input", TextArea)
                composer.load_text("Instead explain only; ask a fresh clarification")
                composer.focus()
                await pilot.press("enter")
            await _question(panel, fresh_question["question"])
            assert panel.chat is chat
            assert panel.query_one("#ai-question-other", Input).value == ""
            assert app.editor.text == original and session.workspace["pages"] == pages
            assert not session.busy and not session.history
            await _choose(pilot, panel, 0)
            await _finished(chat)
            await pilot.pause()
            assert chat.status == "idle"
            assert not panel.query_one("#ai-question").display
            assert app.editor.text == original and session.workspace["pages"] == pages
            assert not session.history and not session.fns
            assert panel.query_one("#ai-apply", Button).disabled
            assert len(requests) == 3
            assert "abandoned-answer" not in json.dumps(panel.messages)
            assert "abandoned-question" not in json.dumps(panel.messages)
            assert "abandoned-apply" not in json.dumps(panel.messages)

    asyncio.run(scenario())
