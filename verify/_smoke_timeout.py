"""Throwaway comparison of healthy streaming against the original timeout bug."""
import asyncio
import importlib.util
import json
import sys

import httpx

from d8r.ai import client


def frame(text=None, finish=None):
    delta = {} if text is None else {"content": text}
    return ("data: " + json.dumps({"choices": [{"index": 0, "delta": delta,
                                                    "finish_reason": finish}]}) + "\n\n").encode()


class ActiveStream(httpx.AsyncByteStream):
    closed = False

    async def __aiter__(self):
        for text in ("Extract ", "the ", "JSON ", "fields ", "without ", "cutting ", "off ", "progress."):
            await asyncio.sleep(0.03)
            yield frame(text)
        yield frame(finish="stop")
        yield b"data: [DONE]\n\n"

    async def aclose(self):
        self.closed = True


async def probe(module):
    stream = ActiveStream()
    original = module.create_client
    module.create_client = lambda config: httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream)
    ))
    history = [{"role": "user", "content": "Extract JSON fields"}]
    try:
        config = module.AIConfig("https://provider.invalid/v1", "smoke", max_attempts=1, timeout=0.12)
        try:
            async for event in module.run_turn(config, history, [], None):
                pass
        except module.AIError:
            assert history == [{"role": "user", "content": "Extract JSON fields"}]
            result = "timed_out"
        else:
            assert history[-1]["content"] == "Extract the JSON fields without cutting off progress."
            result = "completed"
        assert stream.closed
        return result
    finally:
        module.create_client = original


async def main():
    spec = importlib.util.spec_from_file_location("d8r_timeout_baseline", sys.argv[1])
    baseline = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = baseline
    spec.loader.exec_module(baseline)
    before = await probe(baseline)
    after = await probe(client)
    assert before == "timed_out" and after == "completed"
    print(json.dumps({"original_active_stream": before, "fixed_active_stream": after,
                      "stream_duration": "0.24s", "configured_timeout": "0.12s",
                      "partial_history_committed": False}))


asyncio.run(main())
