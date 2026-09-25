"""Offline behavioral coverage for the AI transport and its completion boundary."""

from __future__ import annotations

import asyncio
import json
import traceback
from copy import deepcopy
from dataclasses import replace

import httpx
import pytest

from anyql.ai import client as ai

SECRET = "never-echo-this-api-key"
CONFIG = ai.AIConfig("https://provider.invalid/v1", "test-model", SECRET)
TOOLS = [
    {"type": "function", "function": {"name": "schema", "parameters": {"type": "object"}}},
    {"type": "function", "function": {"name": "history", "parameters": {"type": "object"}}},
]


class Parts(httpx.AsyncByteStream):
    def __init__(self, *parts):
        self.parts = parts
        self.closed = False

    async def __aiter__(self):
        for part in self.parts:
            if isinstance(part, Exception):
                raise part
            yield part

    async def aclose(self):
        self.closed = True


def event(payload, *, newline="\n"):
    return ("data: " + json.dumps(payload, ensure_ascii=False) + newline * 2).encode("utf-8")


def delta(content=None, *, tools=None, finish=None):
    body = {}
    if content is not None:
        body["content"] = content
    if tools is not None:
        body["tool_calls"] = tools
    return {"choices": [{"index": 0, "delta": body, "finish_reason": finish}]}


def tool_call(name="schema", arguments="{}", *, index=0, id="call_a"):
    return {"index": index, "id": id, "type": "function", "function": {"name": name, "arguments": arguments}}


def completed(text="An ordinary answer."):
    return (
        event(delta(text)) + event(delta(finish="stop"))
        + event({"choices": [], "usage": {"total_tokens": 10}}) + b"data: [DONE]\n\n"
    )


def stream_response(*parts):
    return httpx.Response(200, headers={"Content-Type": "text/event-stream; charset=utf-8"}, stream=Parts(*parts))


async def no_tools(name, arguments):
    pytest.fail("An answer without tool calls must not invoke a tool")


async def collect(messages, *, config=CONFIG, tools=(), callback=no_tools, events=None):
    if events is None:
        events = []
    async for item in ai.run_turn(config, messages, list(tools), callback):
        events.append(item)
    return events


@pytest.fixture
def transport(monkeypatch):
    def install(handler):
        requests = []

        def recording(request):
            requests.append(request)
            return handler(request)

        monkeypatch.setattr(
            ai, "create_client",
            lambda config: httpx.AsyncClient(transport=httpx.MockTransport(recording), timeout=config.timeout),
        )
        return requests

    return install


def test_byte_boundaries_multiline_events_and_multiple_streamed_tools(transport):
    calls = []
    final = "Here is the full replacement:\n```anyql\n\\from café\n```"
    first = (
        b"\xef\xbb\xbf: keepalive\r\n\r\n"
        + b'event: message\r\ndata: {"choices": [],\r\ndata: "usage": null}\r\n\r\n'
        + event(delta("Looking at the context.\n"), newline="\r\n")
        + event(delta(tools=[tool_call("history", index=1, id="call_b")]), newline="\r\n")
        + event(delta(tools=[tool_call("sche", '{"ta', id="call_")]), newline="\r\n")
        + event(delta(tools=[{
            "index": 0, "id": "a", "function": {"name": "ma", "arguments": 'ble":"café"}'},
        }]), newline="\r\n")
        + event(delta(finish="tool_calls"), newline="\r\n")
        + b"data: [DONE]\r\n\r\n"
    )
    first_stream = Parts(*(first[i:i + 1] for i in range(len(first))))

    def handler(request):
        assert str(request.url) == "https://provider.invalid/v1/chat/completions"
        assert request.headers["Authorization"] == f"Bearer {SECRET}"
        body = json.loads(request.content)
        assert "response_format" not in body
        if len(requests) == 1:
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=first_stream)
        assert body["messages"][-2:] == [
            {"role": "tool", "tool_call_id": "call_a", "content": '{"columns":["name"]}'},
            {"role": "tool", "tool_call_id": "call_b", "content": "[]"},
        ]
        answer = completed(final)
        return stream_response(*(answer[i:i + 1] for i in range(len(answer))))

    requests = transport(handler)

    async def call(name, arguments):
        assert first_stream.closed  # Calls cannot execute from an unfinished stream.
        calls.append((name, arguments))
        return '{"columns":["name"]}' if name == "schema" else "[]"

    messages = [{"role": "user", "content": "Help me query café"}]
    events = asyncio.run(collect(messages, tools=TOOLS, callback=call))
    assert calls == [("schema", {"table": "café"}), ("history", {})]
    assert messages[-1] == {"role": "assistant", "content": final}
    assert messages[1]["content"] == "Looking at the context.\n"
    assert "".join(item.text for item in events if item.kind == "text") == "Looking at the context.\n" + final
    assert len(requests) == 2


def test_full_completion_url_and_keyless_local_provider(transport):
    def handler(request):
        assert str(request.url) == "http://localhost:8080/v1/chat/completions"
        assert "authorization" not in request.headers
        return stream_response(completed())

    transport(handler)
    messages = []
    config = ai.AIConfig("http://localhost:8080/v1/chat/completions/", "local-model")
    asyncio.run(collect(messages, config=config))
    assert messages == [{"role": "assistant", "content": "An ordinary answer."}]


def test_transient_status_retries_have_capped_delay_and_finite_budget(transport, monkeypatch):
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(ai.asyncio, "sleep", sleep)

    def handler(request):
        if len(requests) == 1:
            return httpx.Response(429, headers={"Retry-After": "99999999"}, text=SECRET)
        if len(requests) == 2:
            return httpx.Response(503, text=SECRET)
        return stream_response(completed("Recovered."))

    requests = transport(handler)
    messages = []
    events = asyncio.run(collect(messages))
    assert delays == [30.0, 2.0]
    assert len(requests) == 3
    assert messages == [{"role": "assistant", "content": "Recovered."}]
    assert all(SECRET not in item.text for item in events)


def test_connect_failure_before_deltas_can_retry(transport, monkeypatch):
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(ai.asyncio, "sleep", sleep)

    def handler(request):
        if len(requests) == 1:
            raise httpx.ConnectTimeout(SECRET, request=request)
        return stream_response(completed("Connected."))

    requests = transport(handler)
    messages = []
    asyncio.run(collect(messages))
    assert len(requests) == 2
    assert delays == [1.0]
    assert messages[-1]["content"] == "Connected."


@pytest.mark.parametrize("tool_rounds", [1, 6])
def test_retry_budget_is_shared_across_tool_rounds(transport, monkeypatch, tool_rounds):
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(ai.asyncio, "sleep", sleep)

    def handler(request):
        if 2 <= len(requests) <= tool_rounds + 1:
            return stream_response(event(delta(tools=[tool_call()], finish="tool_calls")), b"data: [DONE]\n\n")
        return httpx.Response(503, text=SECRET)

    requests = transport(handler)

    async def call(name, arguments):
        return "{}"

    messages = []
    with pytest.raises(ai.AIError) as error:
        asyncio.run(collect(messages, config=replace(CONFIG, max_attempts=2, max_tool_rounds=tool_rounds), tools=TOOLS, callback=call))
    assert len(requests) == tool_rounds + 2
    assert delays == [1.0]
    assert [message["role"] for message in messages] == ["assistant", "tool"] * tool_rounds
    assert SECRET not in str(error.value)


def test_auth_failure_does_not_retry_or_expose_response(transport):
    requests = transport(lambda request: httpx.Response(401, text=f"Your bad key is {SECRET}"))
    messages = []
    with pytest.raises(ai.AIError) as error:
        asyncio.run(collect(messages))
    assert len(requests) == 1
    assert messages == []
    assert SECRET not in str(error.value)
    assert SECRET not in repr(CONFIG)
    assert SECRET not in "".join(traceback.format_exception(error.value))


def test_partial_visible_answer_is_never_replayed_or_committed(transport):
    partial = "```anyql\n\\from users\n```"
    requests = transport(lambda request: stream_response(event(delta(partial)), httpx.ReadError(SECRET)))
    messages = [{"role": "user", "content": "Suggest a query"}]
    events = []
    with pytest.raises(ai.AIError) as error:
        asyncio.run(collect(messages, events=events))
    assert len(requests) == 1
    assert messages == [{"role": "user", "content": "Suggest a query"}]
    assert SECRET not in str(error.value)
    assert SECRET not in "".join(traceback.format_exception(error.value))


@pytest.mark.parametrize("body", [
    event(delta("Looks complete", finish="stop")),  # Missing [DONE].
    event(delta("No finish reason")) + b"data: [DONE]\n\n",
    event(delta("Partial event", finish="stop")) + b"data: [DONE]\n",
    event(delta("Cut off", finish="length")) + b"data: [DONE]\n\n",
    event(delta("Filtered", finish="content_filter")) + b"data: [DONE]\n\n",
    b'data: {"choices": broken}\n\n',
    event({"error": {"message": SECRET}}),
    b"event: error\ndata: " + SECRET.encode() + b"\n\n",
    event({"choices": [{"delta": {"role": []}, "finish_reason": None}]}),
    event({"choices": [{"delta": {}, "finish_reason": {}}]}),
    event({"choices": "invalid"}),
    b"data: \xff\n\n",
    b"data: [DONE]\n\n",
])
def test_truncated_or_malformed_response_never_completes(transport, body):
    requests = transport(lambda request: stream_response(body))
    messages = []
    with pytest.raises(ai.AIError) as error:
        asyncio.run(collect(messages))
    assert len(requests) == 1
    assert messages == []
    assert SECRET not in str(error.value)


def test_invalid_later_tool_arguments_prevent_all_tool_execution(transport):
    body = event(delta(tools=[tool_call(), tool_call(index=1, id="call_b", arguments="[]")], finish="tool_calls"))
    transport(lambda request: stream_response(body, b"data: [DONE]\n\n"))
    messages = []
    with pytest.raises(ai.AIError):
        asyncio.run(collect(messages, tools=TOOLS))
    assert messages == []


def test_unknown_tool_is_rejected_safely_by_callback(transport):
    body = event(delta(tools=[tool_call("shell")], finish="tool_calls"))
    requests = transport(lambda request: stream_response(body, b"data: [DONE]\n\n"))
    seen = []

    async def call(name, arguments):
        seen.append(name)
        raise ValueError(SECRET)

    messages = []
    with pytest.raises(ai.AIError) as error:
        asyncio.run(collect(messages, tools=TOOLS, callback=call))
    assert seen == ["shell"]
    assert len(requests) == 1
    assert messages == []
    assert SECRET not in str(error.value)


@pytest.mark.parametrize("round_limit", [1, 10])
def test_final_answer_after_configured_rounds_keeps_guidance_out_of_history(transport, round_limit):
    calls = []
    original = [
        {"role": "system", "content": "Answer only from collected context."},
        {"role": "user", "content": "Summarize the available context."},
    ]
    messages = deepcopy(original)

    def handler(request):
        body = json.loads(request.content)
        wire_messages = body["messages"]
        if [i for i, message in enumerate(wire_messages) if message["role"] == "system"] != [0]:
            return httpx.Response(400, json={"error": {"message": "System message must be at the beginning."}})
        assert original[0]["content"] in wire_messages[0]["content"]
        if body.get("tool_choice") == "none":
            assert len(calls) == round_limit * 2
            return stream_response(completed(body["messages"][-1]["content"]))
        return stream_response(
            event(delta(tools=[tool_call(), tool_call("history", index=1, id="call_b")], finish="tool_calls")),
            b"data: [DONE]\n\n",
        )

    requests = transport(handler)

    async def call(name, arguments):
        calls.append(name)
        return f"Collected context {len(calls)}"

    config = replace(CONFIG, max_tool_rounds=round_limit, max_tool_calls=2)
    events = asyncio.run(collect(messages, config=config, tools=TOOLS, callback=call))
    assert len(requests) == round_limit + 1
    assert calls == ["schema", "history"] * round_limit
    assert messages[:2] == original
    assert [message["role"] for message in messages[2:]] == ["assistant", "tool", "tool"] * round_limit + ["assistant"]
    assert messages[-1] == {"role": "assistant", "content": f"Collected context {round_limit * 2}"}
    assert "".join(item.text for item in events if item.kind == "text") == f"Collected context {round_limit * 2}"


def test_system_context_survives_retry_and_followup_without_mutating_history(transport, monkeypatch):
    original = [
        {"role": "system", "content": "Use only the supplied schema and never execute a query."},
        {"role": "user", "content": "Explain the schema."},
    ]
    messages = deepcopy(original)
    bodies = []

    async def sleep(delay):
        pass

    monkeypatch.setattr(ai.asyncio, "sleep", sleep)

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        wire_messages = body["messages"]
        if [i for i, message in enumerate(wire_messages) if message["role"] == "system"] != [0]:
            return httpx.Response(400, json={"error": {"message": "System message must be at the beginning."}})
        if len(bodies) == 1:
            return httpx.Response(503)
        return stream_response(completed("The schema contains events."))

    transport(handler)
    asyncio.run(collect(messages, tools=TOOLS))
    assert bodies[0] == bodies[1]
    assert messages[:2] == original
    messages.append({"role": "user", "content": "Which table did you mention?"})
    followup = deepcopy(messages)
    asyncio.run(collect(messages, tools=TOOLS))
    assert bodies[2]["messages"][0] == bodies[0]["messages"][0]
    assert bodies[2]["messages"][0]["content"].count(original[0]["content"]) == 1
    assert bodies[2]["messages"][1:] == followup[1:]
    assert messages[:-1] == followup
    assert messages[-1] == {"role": "assistant", "content": "The schema contains events."}


def test_provider_ignoring_disabled_tools_cannot_exceed_configured_rounds(transport):
    body = event(delta(tools=[tool_call()], finish="tool_calls"))
    requests = transport(lambda request: stream_response(body, b"data: [DONE]\n\n"))
    calls = []

    async def call(name, arguments):
        calls.append(name)
        return "{}"

    messages = []
    with pytest.raises(ai.AIError):
        asyncio.run(collect(messages, config=replace(CONFIG, max_tool_rounds=2), tools=TOOLS, callback=call))
    assert len(requests) == 3
    assert json.loads(requests[-1].content)["tool_choice"] == "none"
    assert calls == ["schema"] * 2
    assert [message["role"] for message in messages] == ["assistant", "tool"] * 2


@pytest.mark.parametrize("final_parts", [
    (event(delta("Looks complete", finish="stop")),),  # Missing [DONE].
    (event(delta("Cut off", finish="length")), b"data: [DONE]\n\n"),
    (b'data: {"choices": broken}\n\n',),
    (event(delta("Partial answer")), httpx.ReadError(SECRET)),
])
def test_final_no_tools_request_still_requires_valid_completion(transport, final_parts):
    calls = []

    def handler(request):
        body = json.loads(request.content)
        if body.get("tool_choice") == "none":
            return stream_response(*final_parts)
        return stream_response(event(delta(tools=[tool_call()], finish="tool_calls")), b"data: [DONE]\n\n")

    requests = transport(handler)

    async def call(name, arguments):
        calls.append(name)
        return "{}"

    messages = []
    with pytest.raises(ai.AIError) as error:
        asyncio.run(collect(messages, config=replace(CONFIG, max_tool_rounds=1), tools=TOOLS, callback=call))
    assert len(requests) == 2
    assert json.loads(requests[-1].content)["tool_choice"] == "none"
    assert calls == ["schema"]
    assert [message["role"] for message in messages] == ["assistant", "tool"]
    assert SECRET not in str(error.value)


def test_over_limit_streamed_tool_batch_executes_nothing(transport):
    requests = transport(lambda request: stream_response(
        event(delta(tools=[tool_call()])),
        event(delta(tools=[tool_call("history", index=1, id="call_b")], finish="tool_calls")),
        b"data: [DONE]\n\n",
    ))
    messages = []
    with pytest.raises(ai.AIError):
        asyncio.run(collect(messages, config=replace(CONFIG, max_tool_calls=1), tools=TOOLS))
    assert messages == []
    assert len(requests) == 1


def test_unterminated_sse_lines_are_size_bounded(transport, monkeypatch):
    monkeypatch.setattr(ai, "MAX_EVENT_BYTES", 64)
    transport(lambda request: stream_response(b"data: " + b"x" * 65))
    messages = []
    with pytest.raises(ai.AIError):
        asyncio.run(collect(messages))
    assert messages == []


def test_streamed_tool_arguments_are_size_bounded(transport, monkeypatch):
    monkeypatch.setattr(ai, "MAX_ARGUMENT_BYTES", 8)
    body = event(delta(tools=[tool_call(arguments='{"long":')]))
    body += event(delta(tools=[{"index": 0, "function": {"arguments": '"value"}'}}], finish="tool_calls"))
    transport(lambda request: stream_response(body, b"data: [DONE]\n\n"))
    messages = []
    with pytest.raises(ai.AIError):
        asyncio.run(collect(messages, tools=TOOLS))
    assert messages == []


def test_keepalive_events_cannot_bypass_total_response_limit(transport, monkeypatch):
    monkeypatch.setattr(ai, "MAX_RESPONSE_BYTES", 64)
    transport(lambda request: stream_response(*(b": ping\n\n" for _ in range(10))))
    messages = []
    with pytest.raises(ai.AIError):
        asyncio.run(collect(messages))
    assert messages == []


def test_answer_limit_counts_utf8_bytes(transport, monkeypatch):
    monkeypatch.setattr(ai, "MAX_CONTENT_BYTES", 4)
    transport(lambda request: stream_response(completed("ééé")))
    messages = []
    with pytest.raises(ai.AIError):
        asyncio.run(collect(messages))
    assert messages == []


def test_absolute_request_timeout_closes_idle_stream(transport):
    class Idle(Parts):
        async def __aiter__(self):
            await asyncio.Event().wait()
            yield b""

    stream = Idle()
    requests = transport(lambda request: httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream))
    messages = []
    with pytest.raises(ai.AIError):
        asyncio.run(collect(messages, config=replace(CONFIG, max_attempts=1, timeout=0.01)))
    assert stream.closed
    assert messages == []
    assert len(requests) == 1


def test_cancellation_closes_stream_without_retries_or_committing(transport):
    async def scenario():
        waiting = asyncio.Event()

        class Blocking(Parts):
            async def __aiter__(self):
                yield event(delta("Partial answer"))
                waiting.set()
                await asyncio.Event().wait()

        stream = Blocking()
        requests = transport(lambda request: httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream))
        messages = []
        task = asyncio.create_task(collect(messages))
        await asyncio.wait_for(waiting.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed
        assert messages == []
        assert len(requests) == 1

    asyncio.run(scenario())


def test_cancellation_mid_tool_batch_keeps_history_consistent(transport):
    body = event(delta(tools=[tool_call(), tool_call("history", index=1, id="call_b")], finish="tool_calls"))
    requests = transport(lambda request: stream_response(body, b"data: [DONE]\n\n"))

    async def scenario():
        waiting = asyncio.Event()

        async def call(name, arguments):
            if name == "schema":
                return "{}"
            waiting.set()
            await asyncio.Event().wait()

        messages = []
        task = asyncio.create_task(collect(messages, tools=TOOLS, callback=call))
        await asyncio.wait_for(waiting.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert messages == []
        assert len(requests) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("changes", [
    {"base_url": "https://user:password@provider.invalid/v1"},
    {"base_url": "https://provider.invalid/v1?key=" + SECRET},
    {"base_url": "https://provider.invalid/v1#" + SECRET},
    {"base_url": "file:///private/model"},
    {"base_url": "https://provider.invalid:99999/v1"},
    {"model": "  "},
    {"api_key": SECRET + "\nHeader: injected"},
    {"max_attempts": 0},
    {"max_attempts": 6},
    {"max_attempts": True},
    {"max_tool_rounds": 0},
    {"max_tool_rounds": 51},
    {"max_tool_rounds": True},
    {"max_tool_calls": 17},
    {"max_tool_calls": 1.5},
    {"sample_rows": 0},
    {"sample_rows": 101},
    {"timeout": float("nan")},
    {"timeout": float("inf")},
    {"timeout": 0},
    {"timeout": 301},
])
def test_invalid_settings_fail_without_exposing_values(changes):
    with pytest.raises(ValueError) as error:
        replace(CONFIG, **changes).validate()
    assert SECRET not in str(error.value)
