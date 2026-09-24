"""Bounded streaming chat, with no provider SDK, persistence, or executable tools."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

MAX_RESPONSE_BYTES = 1_048_576
MAX_EVENT_BYTES = 262_144
MAX_CONTENT_BYTES = 262_144
MAX_ARGUMENT_BYTES = 65_536
MAX_TOOL_RESULT_BYTES = 262_144
MAX_TOOL_CALLS = 16
MAX_TOOL_ROUNDS = 6
MAX_RETRY_DELAY = 30.0


@dataclass(frozen=True)
class AIConfig:
    base_url: str = ""
    model: str = ""
    api_key: str = field(default="", repr=False)
    max_attempts: int = 3
    timeout: float = 60.0

    def validate(self) -> None:
        """Reject invalid settings without including their values in errors."""
        if not isinstance(self.base_url, str) or not self.base_url.strip():
            raise ValueError("An AI provider base URL is required.")
        url = self.base_url.strip()
        if len(url) > 4096 or any(ord(c) < 33 or ord(c) == 127 for c in url) or "\\" in url:
            raise ValueError("The AI provider URL is invalid.")
        try:
            parts = urlsplit(url)
            parsed = httpx.URL(url)
            valid = (
                parts.scheme in {"http", "https"}
                and bool(parts.hostname)
                and bool(parsed.host)
                and (parts.port is None or 1 <= parts.port <= 65535)
                and parts.username is None
                and parts.password is None
                and "?" not in url
                and "#" not in url
            )
        except (ValueError, httpx.InvalidURL):
            valid = False
        if not valid:
            raise ValueError("Use an HTTP(S) AI provider URL without credentials, query, or fragment.")
        if (
            not isinstance(self.model, str)
            or not self.model.strip()
            or len(self.model) > 256
            or any(ord(c) < 32 or ord(c) == 127 for c in self.model)
        ):
            raise ValueError("An AI model name of at most 256 characters is required.")
        if (
            not isinstance(self.api_key, str)
            or len(self.api_key) > 8192
            or any(not 33 <= ord(c) <= 126 for c in self.api_key)
        ):
            raise ValueError("The AI API key must contain only printable non-space ASCII characters.")
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 5:
            raise ValueError("AI attempts must be an integer between 1 and 5.")
        if (
            type(self.timeout) not in {int, float}
            or not 0 < self.timeout <= 300
            or not math.isfinite(self.timeout)
        ):
            raise ValueError("AI timeout must be greater than zero and at most 300 seconds.")


class AIError(Exception):
    """A safe, user-facing transport or protocol error."""


@dataclass
class AIEvent:
    kind: str
    text: str


def create_client(config: AIConfig) -> httpx.AsyncClient:
    """The injection point for offline transports; redirects never receive keys."""
    return httpx.AsyncClient(timeout=config.timeout, follow_redirects=False)


def _size(text: str) -> int:
    try:
        return len(text.encode("utf-8"))
    except UnicodeError:
        raise AIError("The AI provider returned invalid text.") from None


def _reject_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def _object(text: str) -> dict:
    try:
        value = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise AIError("The AI provider returned malformed JSON.") from None
    if not isinstance(value, dict):
        raise AIError("The AI provider returned an invalid response object.")
    return value


async def _sse(response: httpx.Response) -> AsyncIterator[tuple[str, str]]:
    """Frame SSE before decoding UTF-8; even unterminated lines have a byte cap."""
    line = bytearray()
    data: list[str] = []
    event_type = ""
    total = event_size = 0
    skip_lf = False
    first_line = True
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise AIError("The AI response exceeded the size limit.")
        for byte in chunk:
            if skip_lf:
                skip_lf = False
                if byte == 10:
                    continue
            if byte not in {10, 13}:
                line.append(byte)
                if len(line) + event_size > MAX_EVENT_BYTES:
                    raise AIError("An AI stream event exceeded the size limit.")
                continue
            skip_lf = byte == 13
            try:
                text = line.decode("utf-8")
            except UnicodeError:
                raise AIError("The AI stream contained invalid UTF-8.") from None
            event_size += len(line) + 1
            line.clear()
            if event_size > MAX_EVENT_BYTES:
                raise AIError("An AI stream event exceeded the size limit.")
            if first_line:
                text = text.removeprefix("\ufeff")
                first_line = False
            if not text:
                if data:
                    yield event_type, "\n".join(data)
                data = []
                event_type = ""
                event_size = 0
            elif not text.startswith(":"):
                name, separator, value = text.partition(":")
                if separator and value.startswith(" "):
                    value = value[1:]
                if name == "data":
                    data.append(value)
                elif name == "event":
                    event_type = value
    # A final line/event without its blank delimiter is not a complete SSE event.


@dataclass
class _ToolCall:
    id: str = ""
    name: str = ""
    arguments: list[str] = field(default_factory=list)
    argument_size: int = 0

    def add(self, fragment: dict) -> None:
        if fragment.get("type") not in (None, "function"):
            raise AIError("The AI provider requested an unsupported tool type.")
        function = fragment.get("function", {})
        if not isinstance(function, dict):
            raise AIError("The AI provider returned an invalid tool call.")
        for key, value in (("id", fragment.get("id")), ("name", function.get("name"))):
            if value is None:
                continue
            if not isinstance(value, str):
                raise AIError("The AI provider returned an invalid tool identifier.")
            current = getattr(self, key) + value
            if _size(current) > 256 or any(ord(c) < 33 or ord(c) == 127 for c in current):
                raise AIError("The AI provider returned an invalid tool identifier.")
            setattr(self, key, current)
        arguments = function.get("arguments")
        if arguments is not None:
            if not isinstance(arguments, str):
                raise AIError("The AI provider returned invalid tool arguments.")
            self.argument_size += _size(arguments)
            if self.argument_size > MAX_ARGUMENT_BYTES:
                raise AIError("AI tool arguments exceeded the size limit.")
            self.arguments.append(arguments)


@dataclass
class _Response:
    started: bool = False
    content: list[str] = field(default_factory=list)
    content_size: int = 0
    calls: dict[int, _ToolCall] = field(default_factory=dict)
    finish: str | None = None

    def accept(self, payload: dict) -> str:
        if "error" in payload:
            raise AIError("The AI provider reported a streaming error.")
        choices = payload.get("choices")
        if not isinstance(choices, list):
            raise AIError("The AI provider returned an invalid streaming response.")
        if not choices:  # Optional usage-only chunk, including after the finish chunk.
            return ""
        if len(choices) != 1 or not isinstance(choices[0], dict):
            raise AIError("The AI provider returned unexpected response choices.")
        choice = choices[0]
        if choice.get("index", 0) != 0 or self.finish is not None:
            raise AIError("The AI provider returned an unexpected response sequence.")
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            raise AIError("The AI provider returned an invalid response delta.")
        self.started |= bool(delta)
        if delta.get("role") not in (None, "assistant"):
            raise AIError("The AI provider returned an unexpected response role.")
        if delta.get("refusal") or "function_call" in delta:
            raise AIError("The AI provider refused the request or used an unsupported response format.")
        content = delta.get("content")
        if content is not None:
            if not isinstance(content, str):
                raise AIError("The AI provider returned invalid response content.")
            self.content_size += _size(content)
            if self.content_size > MAX_CONTENT_BYTES:
                raise AIError("The AI answer exceeded the size limit.")
            self.content.append(content)
        fragments = delta.get("tool_calls", [])
        if not isinstance(fragments, list):
            raise AIError("The AI provider returned invalid tool calls.")
        for fragment in fragments:
            if not isinstance(fragment, dict):
                raise AIError("The AI provider returned an invalid tool call.")
            index = fragment.get("index")
            if type(index) is not int or not 0 <= index < MAX_TOOL_CALLS:
                raise AIError("The AI provider exceeded the tool-call limit or returned an invalid index.")
            if index not in self.calls:
                self.calls[index] = _ToolCall()
            self.calls[index].add(fragment)
        finish = choice.get("finish_reason")
        if finish is not None:
            if finish not in ("stop", "tool_calls"):
                raise AIError("The AI response was cut short or blocked; no proposal is available.")
            if (finish == "tool_calls") != bool(self.calls):
                raise AIError("The AI provider returned an incomplete tool response.")
            self.finish = finish
        return content or ""

    def complete(self) -> tuple[dict, list[tuple[str, dict]]]:
        if self.finish is None:
            raise AIError("The AI stream ended without a complete response.")
        message: dict = {"role": "assistant", "content": "".join(self.content)}
        requests: list[tuple[str, dict]] = []
        if self.calls:
            if sorted(self.calls) != list(range(len(self.calls))):
                raise AIError("The AI provider returned incomplete tool calls.")
            tool_calls = []
            ids: set[str] = set()
            for index in sorted(self.calls):
                call = self.calls[index]
                if not call.id or not call.name or call.id in ids:
                    raise AIError("The AI provider returned incomplete or duplicate tool identifiers.")
                ids.add(call.id)
                arguments = "".join(call.arguments)
                parsed = _object(arguments)
                tool_calls.append({
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": arguments},
                })
                requests.append((call.name, parsed))
            message["tool_calls"] = tool_calls
            message["content"] = message["content"] or None
        elif not message["content"].strip():
            raise AIError("The AI provider returned an empty answer.")
        return message, requests


def _retry_after(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        seconds = float(value)
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            seconds = (date - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return 0.0
    if not math.isfinite(seconds):
        return 0.0
    return max(0.0, min(seconds, MAX_RETRY_DELAY))


class _Retryable(Exception):
    def __init__(self, message: str, delay: float = 0.0):
        self.message = message
        self.delay = delay


async def run_turn(
    config: AIConfig,
    messages: list[dict],
    tools: list[dict],
    call_tool: Callable[[str, dict], Awaitable[str]],
) -> AsyncIterator[AIEvent]:
    """Stream a turn; only normal iterator exhaustion makes the answer complete.

    Text may include narration before a tool round. The final assistant message
    in ``messages`` is the final answer. Failed responses never enter history;
    completed tool exchanges are appended atomically. The caller may use a copy
    of its history to discard the whole turn on cancellation or failure.

    ``max_attempts - 1`` is a shared retry budget, not a budget reset per tool
    round. Up to six successful tool rounds may precede the final answer.
    """
    try:
        config.validate()
    except ValueError as exc:
        raise AIError(str(exc)) from None
    url = config.base_url.strip().rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    headers = {"Accept": "text/event-stream"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    retries = rounds = 0
    async with create_client(config) as client:
        while True:
            state = _Response()
            payload = {"model": config.model.strip(), "messages": messages, "stream": True}
            if tools:
                payload["tools"] = tools
            failure: _Retryable | None = None
            try:
                async with asyncio.timeout(config.timeout):
                    async with client.stream("POST", url, headers=headers, json=payload) as response:
                        status = response.status_code
                        if status in {408, 429} or 500 <= status <= 599:
                            raise _Retryable(
                                f"The AI provider is temporarily unavailable (HTTP {status}).",
                                _retry_after(response.headers.get("retry-after")),
                            )
                        if status in {401, 403}:
                            raise AIError(f"The AI provider rejected authentication (HTTP {status}). Check the API key.")
                        if status != 200:
                            raise AIError(f"The AI provider rejected the request (HTTP {status}). Check its URL and model.")
                        if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "text/event-stream":
                            raise AIError("The AI provider did not return an event stream.")
                        done = False
                        async for event_type, data in _sse(response):
                            if event_type == "error":
                                raise AIError("The AI provider reported a streaming error.")
                            if data.strip() == "[DONE]":
                                done = True
                                break
                            text = state.accept(_object(data))
                            if text:
                                yield AIEvent("text", text)
                        if not done:
                            raise AIError("The AI stream was interrupted before completion; no proposal is available.")
                        message, requests = state.complete()
            except _Retryable as exc:
                failure = exc
            except (httpx.TransportError, TimeoutError) as exc:
                reason = "The AI request timed out." if isinstance(exc, (httpx.TimeoutException, TimeoutError)) else "The AI connection failed."
                if state.started:
                    raise AIError(reason + " The partial answer was discarded; retry manually.") from None
                failure = _Retryable(reason)
            except httpx.HTTPError:
                raise AIError("The AI request failed.") from None
            if failure is not None:
                if retries >= config.max_attempts - 1:
                    raise AIError(failure.message + " Automatic attempts exhausted.") from None
                delay = min(MAX_RETRY_DELAY, max(2.0 ** retries, failure.delay))
                retries += 1
                yield AIEvent("status", f"Retrying AI request in {delay:g}s ({retries + 1}/{config.max_attempts}).")
                await asyncio.sleep(delay)
                continue
            if not requests:
                messages.append(message)
                return
            if rounds >= MAX_TOOL_ROUNDS:
                raise AIError("The AI reached the context-tool round limit; no final answer is available.")
            yield AIEvent("status", "Reading requested AI context…")
            results = []
            for tool_call, (name, arguments) in zip(message["tool_calls"], requests):
                try:
                    async with asyncio.timeout(config.timeout):
                        result = await call_tool(name, arguments)
                except Exception:
                    raise AIError("An AI context tool failed; no final answer is available.") from None
                if not isinstance(result, str) or _size(result) > MAX_TOOL_RESULT_BYTES:
                    raise AIError("An AI context tool returned an invalid or oversized result.")
                results.append({"role": "tool", "tool_call_id": tool_call["id"], "content": result})
            messages.extend([message, *results])
            rounds += 1
