"""AI acceptance: real Textual widgets and engine, deterministic provider wire."""
from __future__ import annotations

import asyncio
import json
import threading

import httpx
from textual.widgets import Button, Input, OptionList, Static, TextArea

from anyql.ai import client
from anyql.ai.client import AIConfig
from anyql.ai.context import AIContext
from anyql.tui.ai import AIPanel
from anyql.tui.app import AnyqlApp
from anyql.tui.fn import FnScreen
from anyql.tui.session import Session
from anyql.tui.settings import AIProviderScreen, SettingsScreen

PROPOSAL = "\\from events\n\\select event_type\n\\limit 2"
ANSWER = "Here is the replacement.\n```anyql\n" + PROPOSAL + "\n```"


def frame(delta: dict, finish=None) -> bytes:
    return ("data: " + json.dumps({"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n").encode()


def completion(text=ANSWER) -> bytes:
    return frame({"content": text}) + frame({}, "stop") + b"data: [DONE]\n\n"


def install_provider(monkeypatch, handler):
    def strict_provider(request):
        messages = json.loads(request.content)["messages"]
        if [i for i, message in enumerate(messages) if message["role"] == "system"] != [0]:
            return httpx.Response(400, json={"error": {"message": "System message must be at the beginning."}})
        return handler(request)

    transport = httpx.MockTransport(strict_provider)
    monkeypatch.setattr(client, "create_client", lambda config: httpx.AsyncClient(transport=transport))


async def finished(panel):
    async def wait():
        while panel.busy:
            await asyncio.sleep(0.01)
    await asyncio.wait_for(wait(), 8)


async def click(pilot, widget):
    widget.scroll_visible(animate=False)
    await pilot.pause()
    await pilot.click(widget)


def configured():
    session = Session()
    session.ai_config = AIConfig(base_url="https://provider.invalid/v1", model="test", api_key="test-secret")
    return session


def test_settings_save_cancel_and_masked_key():
    async def scenario():
        app = AnyqlApp()
        async with app.run_test(size=(150, 58)) as pilot:
            await pilot.press("ctrl+comma")
            screen = app.screen
            assert isinstance(screen, SettingsScreen)
            sidebar = screen.query_one("#settings-sidebar", OptionList)
            sidebar.highlighted = sidebar.get_option_index("ai")
            await pilot.pause()
            await pilot.press("enter", "enter")
            assert isinstance(app.screen, AIProviderScreen)
            form = app.screen
            form.query_one("#ai-base-url", Input).value = "https://provider.invalid/v1"
            form.query_one("#ai-model", Input).value = "test"
            form.query_one("#ai-api-key", Input).value = "private-value"
            form.query_one("#ai-attempts", Input).value = "4"
            form.query_one("#ai-timeout", Input).value = "45"
            form.query_one("#ai-tool-rounds", Input).value = "50"
            form.query_one("#ai-tool-calls", Input).value = "7"
            form.query_one("#ai-sample-rows", Input).value = "100"
            assert form.query_one("#ai-api-key", Input).password
            await click(pilot, form.query_one("#ai-settings-save", Button))
            assert app.session.ai_config.model == "test"
            assert app.session.ai_config.api_key == "private-value"
            assert "private-value" not in repr(app.session.ai_config)
            await pilot.press("enter")
            form = app.screen
            assert isinstance(form, AIProviderScreen)
            form.query_one("#ai-model", Input).value = "cancelled-change"
            form.query_one("#ai-tool-rounds", Input).value = "1"
            form.query_one("#ai-tool-calls", Input).value = "1"
            form.query_one("#ai-sample-rows", Input).value = "1"
            await pilot.press("escape")
            assert app.session.ai_config.model == "test"
        restored = AnyqlApp()
        async with restored.run_test(size=(150, 58)) as pilot:
            restored.push_screen(AIProviderScreen(restored))
            await pilot.pause()
            form = restored.screen
            assert form.query_one("#ai-base-url", Input).value == "https://provider.invalid/v1"
            assert form.query_one("#ai-model", Input).value == "test"
            assert form.query_one("#ai-api-key", Input).value == "private-value"
            assert form.query_one("#ai-api-key", Input).password
            assert form.query_one("#ai-attempts", Input).value == "4"
            assert float(form.query_one("#ai-timeout", Input).value) == 45
            assert form.query_one("#ai-tool-rounds", Input).value == "50"
            assert form.query_one("#ai-tool-calls", Input).value == "7"
            assert form.query_one("#ai-sample-rows", Input).value == "100"
    asyncio.run(scenario())


def test_settings_edits_selected_provider_field_and_returns_to_it():
    async def scenario():
        session = configured()
        session.update_settings(ai=session.ai_config)
        app = AnyqlApp(session=session)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("ctrl+comma")
            screen = app.screen
            sidebar = screen.query_one("#settings-sidebar", OptionList)
            sidebar.highlighted = sidebar.get_option_index("ai")
            await pilot.pause()
            await pilot.press("enter")
            menu = screen.query_one("#settings-menu", OptionList)
            for field, attribute, value, cancelled in (
                ("ai-model", "model", "new", "cancelled"),
                ("ai-tool-rounds", "max_tool_rounds", "1", "2"),
                ("ai-tool-calls", "max_tool_calls", "1", "2"),
                ("ai-sample-rows", "sample_rows", "1", "2"),
            ):
                menu.highlighted = next(index for index, row in enumerate(screen.rows) if row.value == field)
                previous = getattr(Session().ai_config, attribute)
                await pilot.press("enter")
                assert isinstance(app.screen, AIProviderScreen)
                form = app.screen
                control = form.query_one(f"#{field}", Input)
                assert app.focused is control
                await pilot.press("home", "shift+end", *value)
                assert control.value == value
                assert getattr(Session().ai_config, attribute) == previous
                await click(pilot, form.query_one("#ai-settings-save", Button))
                assert app.screen is screen
                assert menu.has_focus
                assert screen.rows[menu.highlighted].value == field
                assert str(getattr(Session().ai_config, attribute)) == value
                await pilot.press("enter")
                assert app.focused is app.screen.query_one(f"#{field}", Input)
                await pilot.press("home", "shift+end", *cancelled, "escape")
                assert app.screen is screen
                assert str(getattr(Session().ai_config, attribute)) == value
    asyncio.run(scenario())


def test_provider_fields_remain_keyboard_reachable_on_small_screen():
    async def scenario():
        app = AnyqlApp()
        async with app.run_test(size=(80, 24)) as pilot:
            app.push_screen(AIProviderScreen(app))
            await pilot.pause()
            form = app.screen
            for field in ("ai-base-url", "ai-model", "ai-api-key", "ai-tool-rounds",
                          "ai-tool-calls", "ai-sample-rows", "ai-attempts", "ai-timeout"):
                control = form.query_one(f"#{field}", Input)
                assert app.focused is control
                assert await pilot.click(control)
                await pilot.press("tab")
            for button in ("ai-settings-save", "ai-settings-import", "ai-settings-cancel"):
                assert app.focused is form.query_one(f"#{button}", Button)
                if button != "ai-settings-cancel":
                    await pilot.press("tab")
            await pilot.press("enter")
            assert app.screen is not form
            assert not app.session.settings_path.exists()
    asyncio.run(scenario())


def test_invalid_provider_budgets_block_atomic_save_without_exposing_values():
    async def scenario():
        session = configured()
        session.update_settings(ai=session.ai_config)
        original = session.ai_config
        saved = session.settings_path.read_bytes()
        app = AnyqlApp(session=session)
        async with app.run_test(size=(80, 24)) as pilot:
            app.push_screen(AIProviderScreen(app))
            await pilot.pause()
            form = app.screen
            form.query_one("#ai-model", Input).value = "draft-model"
            form.query_one("#ai-api-key", Input).value = "private-unsaved-key"
            drafts = {"ai-tool-rounds": "20", "ai-tool-calls": "8", "ai-sample-rows": "30"}
            for field, value in drafts.items():
                form.query_one(f"#{field}", Input).value = value
            for field, invalid in (
                ("ai-tool-rounds", "0"), ("ai-tool-rounds", "51"),
                ("ai-tool-calls", "0"), ("ai-tool-calls", "17"),
                ("ai-sample-rows", "0"), ("ai-sample-rows", "101"),
                ("ai-sample-rows", "private-unsaved-key"),
            ):
                form.query_one(f"#{field}", Input).value = invalid
                await click(pilot, form.query_one("#ai-settings-save", Button))
                # Textual ignores repeat presses during the button's active effect.
                await pilot.pause(form.query_one("#ai-settings-save", Button).active_effect_duration)
                assert app.screen is form
                assert session.ai_config == original
                assert session.settings_path.read_bytes() == saved
                assert form.query_one("#ai-model", Input).value == "draft-model"
                assert form.query_one("#ai-api-key", Input).value == "private-unsaved-key"
                error = str(form.query_one("#ai-settings-error", Static).content)
                assert error
                assert "private-unsaved-key" not in error
                form.query_one(f"#{field}", Input).value = drafts[field]
            await click(pilot, form.query_one("#ai-settings-save", Button))
            assert app.screen is not form
            assert Session().ai_config == AIConfig(
                base_url=original.base_url, model="draft-model", api_key="private-unsaved-key",
                max_tool_rounds=20, max_tool_calls=8, sample_rows=30,
            )
    asyncio.run(scenario())


def test_provider_save_failure_keeps_form_and_previous_settings():
    async def scenario():
        app = AnyqlApp()
        async with app.run_test(size=(150, 58)) as pilot:
            app.push_screen(AIProviderScreen(app))
            await pilot.pause()
            form = app.screen
            form.query_one("#ai-base-url", Input).value = "https://provider.invalid/v1"
            form.query_one("#ai-model", Input).value = "unsaved"
            form.query_one("#ai-api-key", Input).value = "private-unsaved-key"
            Session().update_settings(intellisense=False)
            await click(pilot, form.query_one("#ai-settings-save", Button))
            assert app.screen is form
            assert app.session.ai_config.api_key == ""
            assert form.query_one("#ai-api-key", Input).password
            message = str(form.query_one("#ai-settings-error", Static).content)
            assert message
            assert "private-unsaved-key" not in message
            restored = Session()
            assert restored.ai_config.api_key == ""
            assert restored.intellisense is False
    asyncio.run(scenario())


def test_import_provider_then_cancel_does_not_save(monkeypatch):
    config = AIConfig(base_url="https://provider.invalid/v1", model="imported", api_key="imported-private-key")
    monkeypatch.setattr("anyql.tui.settings.import_yolo_config", lambda: config)

    async def scenario():
        app = AnyqlApp()
        async with app.run_test(size=(150, 58)) as pilot:
            app.push_screen(AIProviderScreen(app))
            await pilot.pause()
            form = app.screen
            drafts = {"ai-tool-rounds": "22", "ai-tool-calls": "8", "ai-sample-rows": "31",
                      "ai-attempts": "4", "ai-timeout": "45"}
            for field, value in drafts.items():
                form.query_one(f"#{field}", Input).value = value
            await click(pilot, form.query_one("#ai-settings-import", Button))
            assert form.query_one("#ai-api-key", Input).value == config.api_key
            assert form.query_one("#ai-api-key", Input).password
            assert {field: form.query_one(f"#{field}", Input).value for field in drafts} == drafts
            assert not app.session.settings_path.exists()
            await pilot.press("escape")
            assert Session().ai_config.api_key == ""
            assert Session().ai_config == AIConfig()
            assert not app.session.settings_path.exists()
    asyncio.run(scenario())


def test_inline_stream_tools_and_apply_only(monkeypatch):
    async def scenario():
        release = asyncio.Event()
        streamed = asyncio.Event()
        requests = []

        class ResponseStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield frame({"content": ANSWER})
                streamed.set()
                await release.wait()
                yield frame({}, "stop") + b"data: [DONE]\n\n"

        def provider(request):
            body = json.loads(request.content)
            requests.append(body)
            tool_results = [m for m in body["messages"] if m["role"] == "tool"]
            if len(requests) == 1:
                return httpx.Response(429, headers={"Retry-After": "0"})
            if not tool_results:
                calls = [{"index": i, "id": f"call-{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
                         for i, (name, args) in enumerate((("schema", {"table": "events"}), ("sample_rows", {"table": "events", "limit": 2}), ("query_history", {})))]
                return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=frame({"tool_calls": calls}) + frame({}, "tool_calls") + b"data: [DONE]\n\n")
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=ResponseStream())

        install_provider(monkeypatch, provider)
        session = configured()
        session.run("\\from events\n\\select user_id\n\\limit 1")
        app = AnyqlApp(session)
        async with app.run_test(size=(160, 62)) as pilot:
            app.editor.load_text("")
            app.editor.focus()
            await pilot.press("\\", "A", "I", "enter")
            panel = app.query_one("#workspace-ai", AIPanel)
            assert panel.display
            original = app.editor.text
            assert "\\AI" not in original
            composer = panel.query_one("#ai-input", TextArea)
            composer.load_text("Use schema, sample rows and history.")
            composer.move_cursor((0, len(composer.text)))
            composer.focus()
            await pilot.press("shift+enter", *"Propose a query.")
            assert composer.text == "Use schema, sample rows and history.\nPropose a query."
            assert not requests  # Shift+Enter edits the message; it does not send.
            await pilot.press("enter")
            await asyncio.wait_for(streamed.wait(), 8)
            await pilot.pause()
            assert "replacement" in str(panel.query_one("#ai-transcript", Static).content)
            assert panel.query_one("#ai-apply", Button).disabled
            assert app.editor.text == original
            assert len(session.history) == 1  # ctrl+enter sent chat, not a query
            assert len(requests) == 3  # rate limit, successful tools, final stream
            sent = next(message for message in requests[0]["messages"] if message["role"] == "user")
            assert sent["content"] == "Use schema, sample rows and history.\nPropose a query."
            assert composer.text == ""
            results = [json.loads(m["content"]) for m in requests[-1]["messages"] if m["role"] == "tool"]
            assert results[0]["tables"][0]["name"] == "events"
            assert len(results[1]["rows"]) == 2
            assert results[2]["history"][0]["document"].startswith("\\from events")
            release.set()
            await finished(panel)
            assert not panel.query_one("#ai-apply", Button).disabled
            assert app.editor.text == original
            await click(pilot, panel.query_one("#ai-apply", Button))
            assert app.editor.text == PROPOSAL
            assert len(session.history) == 1
            panel.query_one("#ai-input", TextArea).focus()
            await pilot.press("tab")
            assert app.focused is panel.query_one("#ai-send", Button)
    asyncio.run(scenario())


def test_failed_tool_attempts_remain_copyable_without_entering_chat_history(monkeypatch):
    requests = []
    attempted = "\\from events\n\\select date_trunc(timestamp) as unsupported_attempt\n\\select 'test-secret', 'd1-private-token'"

    def provider(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            call = {"index": 0, "id": "validate-attempt", "type": "function", "function": {
                "name": "validate_anyql", "arguments": json.dumps({"text": attempted}),
            }}
            data = frame({"tool_calls": [call]}, "tool_calls") + b"data: [DONE]\n\n"
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=data)
        if len(requests) == 2:
            return httpx.Response(503, text="provider-body-must-not-be-logged test-secret")
        assert "unsupported_attempt" not in json.dumps(body["messages"])
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=completion())

    install_provider(monkeypatch, provider)

    async def scenario():
        session = configured()
        session.ai_config = AIConfig("https://provider.invalid/v1", "test", "test-secret", max_attempts=1)
        session.d1_profiles = [{"account_id": "saved-account", "database": "11111111-2222-3333-4444-555555555555",
                                "display": "Saved D1", "api_token": "d1-private-token"}]
        app = AnyqlApp(session)
        async with app.run_test(size=(120, 40)) as pilot:
            app.editor.focus()
            await pilot.press("\\", "A", "I", "enter")
            panel = app.query_one("#workspace-ai", AIPanel)
            original = app.editor.text
            panel.query_one("#ai-input", TextArea).load_text("Count requests by day")
            panel.action_send()
            await finished(panel)
            assert not panel.messages
            assert panel.query_one("#ai-apply", Button).disabled
            assert app.editor.text == original
            await click(pilot, panel.query_one("#ai-logs", Button))
            logs = app.screen.query_one("#ai-log-text", TextArea).text
            assert "unsupported_attempt" in logs and "date_trunc" in logs
            assert '"valid": false' in logs
            assert "HTTP 503" in logs
            assert "test-secret" not in logs and "d1-private-token" not in logs
            assert "[REDACTED]" in logs
            assert "provider-body-must-not-be-logged" not in logs
            await click(pilot, app.screen.query_one("#ai-log-copy", Button))
            assert app.clipboard == logs
            await pilot.press("escape")
            panel.query_one("#ai-input", TextArea).load_text("Try again")
            panel.action_send()
            await finished(panel)
            assert not panel.query_one("#ai-apply", Button).disabled
            await click(pilot, panel.query_one("#ai-logs", Button))
            logs = app.screen.query_one("#ai-log-text", TextArea).text
            assert "unsupported_attempt" in logs and "Turn 2 started" in logs
            await pilot.press("escape")
            panel.action_clear()
            await click(pilot, panel.query_one("#ai-logs", Button))
            assert "unsupported_attempt" not in app.screen.query_one("#ai-log-text", TextArea).text
            assert not session.history
    asyncio.run(scenario())


def test_cancellation_stale_edits_and_source_isolation(monkeypatch):
    async def scenario():
        partial = asyncio.Event()
        close_seen = asyncio.Event()
        requests = []

        class Pending(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield frame({"content": ANSWER})
                partial.set()
                await asyncio.Event().wait()
            async def aclose(self):
                close_seen.set()

        def provider(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=Pending()) if len(requests) == 1 else httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=completion())

        install_provider(monkeypatch, provider)
        app = AnyqlApp(configured())
        async with app.run_test(size=(160, 62)) as pilot:
            app.action_ai()
            panel = app.query_one("#workspace-ai", AIPanel)
            original = app.editor.text
            panel.query_one("#ai-input", TextArea).load_text("first")
            await click(pilot, panel.query_one("#ai-send", Button))
            await asyncio.wait_for(partial.wait(), 5)
            await click(pilot, panel.query_one("#ai-cancel", Button))
            await asyncio.wait_for(close_seen.wait(), 5)
            assert app.editor.text == original
            assert panel.query_one("#ai-apply", Button).disabled
            assert not panel.messages
            panel.query_one("#ai-input", TextArea).load_text("second")
            await click(pilot, panel.query_one("#ai-send", Button))
            await finished(panel)
            assert not panel.query_one("#ai-apply", Button).disabled
            app.editor.load_text("\\from users\n\\limit 1")
            await pilot.pause()
            panel.action_apply()
            assert app.editor.text == "\\from users\n\\limit 1"
            assert panel.query_one("#ai-apply", Button).disabled
            app.select_source("mysql")
            await pilot.pause()
            assert not panel.messages
            assert not panel.query_one("#ai-transcript", Static).content
    asyncio.run(scenario())


def test_cancelled_ai_sample_keeps_connection_reserved_until_execution_finishes(monkeypatch):
    from anyql.ai import context as context_module

    started = threading.Event()
    release = threading.Event()
    execute = context_module.execute

    def delayed_execute(*args, **kwargs):
        started.set()
        if not release.wait(10):
            raise RuntimeError("sample was not released after chat cancellation")
        return execute(*args, **kwargs)

    def provider(request):
        call = {"index": 0, "id": "sample", "type": "function", "function": {
            "name": "sample_rows", "arguments": json.dumps({"table": "events", "limit": 2}),
        }}
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                             content=frame({"tool_calls": [call]}) + frame({}, "tool_calls") + b"data: [DONE]\n\n")

    monkeypatch.setattr(context_module, "execute", delayed_execute)
    install_provider(monkeypatch, provider)

    async def scenario():
        app = AnyqlApp(configured())
        async with app.run_test(size=(160, 62)) as pilot:
            app.editor.load_text(PROPOSAL)
            app.action_ai()
            panel = app.ai_panel
            panel.query_one("#ai-input", TextArea).load_text("Read a sample.")
            try:
                await click(pilot, panel.query_one("#ai-send", Button))
                assert await asyncio.to_thread(started.wait, 2)
                await click(pilot, panel.query_one("#ai-cancel", Button))
                await click(pilot, panel.query_one("#ai-close", Button))
                await pilot.press("ctrl+enter")
                assert not app.run_busy
                assert "unavailable" in str(app.query_one("#status", Static).content)
                assert not app.session.history

                release.set()
                async def idle():
                    while app.session.busy:
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(idle(), 5)
                await pilot.press("ctrl+enter")
                await app.workers.wait_for_complete()
                assert [entry.doc for entry in app.session.history] == [PROPOSAL]
                assert app.session.history[0].rows == 2
                assert not panel.messages
            finally:
                release.set()
                await asyncio.gather(*panel._context_tasks)

    asyncio.run(scenario())


def test_function_make_ai_uses_draft_and_requires_apply_then_save(monkeypatch):
    requests = []
    body = "\\from products\n\\where category = @category\n\\select category\n\\limit 2"
    metadata = {"name": "categories", "description": "Return two product categories",
                "parameters": ["category"], "arguments": '"electronics"'}

    def provider(request):
        requests.append(json.loads(request.content))
        answer = "```json\n" + json.dumps(metadata) + "\n```\n```anyql\n" + body + "\n```"
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=completion(answer))

    install_provider(monkeypatch, provider)

    async def scenario():
        from textual.widgets import Select
        app = AnyqlApp(configured())
        async with app.run_test(size=(100, 35)) as pilot:
            app.action_fn()
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, FnScreen)
            await click(pilot, screen.query_one("#fn-ai", Button))
            panel = screen.ai_panel
            assert not requests  # Opening the helper must not send anything.
            assert app.focused is panel.query_one("#ai-input", TextArea)
            screen.query_one("#fn-source", Select).value = "mysql"
            await pilot.pause()
            panel.query_one("#ai-input", TextArea).load_text("Give me two products in a category I choose")
            panel.query_one("#ai-input", TextArea).focus()
            await pilot.press("ctrl+enter")
            await finished(panel)
            assert screen.query_one("#fn-name", Input).value == ""
            assert screen.query_one("#fn-body", TextArea).text == ""
            assert not app.session.fns
            assert not panel.query_one("#ai-apply", Button).disabled
            assert "categories(category)" in str(panel.query_one("#ai-function-details", Static).content)
            # Even example-argument edits make an outstanding replacement stale.
            screen.query_one("#fn-args", Input).value = '"books"'
            await pilot.pause()
            panel.action_apply()
            assert screen.query_one("#fn-body", TextArea).text == ""
            assert panel.query_one("#ai-apply", Button).disabled
            panel.query_one("#ai-input", TextArea).load_text("Use electronics as the example instead")
            panel.query_one("#ai-input", TextArea).focus()
            await pilot.press("ctrl+enter")
            await finished(panel)
            assert len(requests) == 2
            assert not panel.query_one("#ai-apply", Button).disabled
            await click(pilot, panel.query_one("#ai-apply", Button))
            assert not panel.display
            assert screen.query_one("#fn-name", Input).value == "categories"
            assert screen.query_one("#fn-params", Input).value == "category"
            assert screen.query_one("#fn-args", Input).value == '"electronics"'
            assert screen.query_one("#fn-body", TextArea).text == body
            assert not app.session.fns
            assert not app.session.history
            assert not app.session.storage_path.exists()
            await click(pilot, screen.query_one("#fn-save", Button))
            assert app.session.fns["categories"].body == body
            assert app.session.active_id == "demo"
    asyncio.run(scenario())


def test_context_pins_source_bounds_samples_and_never_runs_suggested_text():
    async def scenario():
        session = Session()
        session.run("\\from events\n\\limit 1")
        context = AIContext(session, "demo", "")
        session.set_active("mysql")
        session.run("\\from products\n\\limit 1")
        history = json.loads(await context.call_tool("query_history", {}))
        assert [h["document"] for h in history["history"]] == ["\\from events\n\\limit 1"]
        sampled = json.loads(await context.call_tool("sample_rows", {"table": "events", "limit": 5}))
        assert len(sampled["rows"]) == 5
        assert "event_type" in sampled["columns"]
        assert "error" in json.loads(await context.call_tool("sample_rows", {"table": "products"}))
        assert "error" in json.loads(await context.call_tool("sample_rows", {"table": "events", "limit": 10000}))
        assert "error" in json.loads(await context.call_tool("execute", {"text": "\\drop events"}))
        assert context.validate_proposal(PROPOSAL) is None
        assert context.validate_proposal("\\from events\n\\bogus") is not None
        fn_context = AIContext(session, "demo", "", parameters=("kind",))
        assert fn_context.validate_proposal("\\from events\n\\where event_type = @kind") is None
        assert fn_context.validate_proposal("\\from events\n\\where event_type = @missing") is not None
        assert fn_context.validate_proposal("\\from events\n\\temp kept") is not None
        assert session.active_id == "mysql"
        assert len(session.history) == 2
        assert "events" in session.sources["demo"].datasets
    asyncio.run(scenario())


def test_complete_function_proposals_reject_invalid_or_conflicting_definitions():
    session = Session()
    session.save_fn("existing", "", PROPOSAL, "Saved work")
    context = AIContext(session, "demo", "", parameters=())

    def proposal(name="generated", parameters=None, body=PROPOSAL):
        metadata = {"name": name, "description": "Example", "parameters": parameters or [], "arguments": ""}
        return context.read_proposal("```json\n" + json.dumps(metadata) + "\n```\n```anyql\n" + body + "\n```")

    assert context.validate_replacement(proposal()) is None
    assert context.validate_replacement(proposal(name="existing")) is not None
    assert context.validate_replacement(proposal(parameters=["a", "a"])) is not None
    assert context.validate_replacement(proposal(parameters=["a,b"])) is not None
    assert context.validate_replacement(proposal(body="\\from events\n\\where amount > @missing")) is not None
    assert context.validate_replacement(proposal(body="\\from events\n\\temp kept")) is not None
    assert list(session.fns) == ["existing"]
    assert not session.history
