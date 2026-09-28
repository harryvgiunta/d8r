"""The AI's cents conversion validates, applies, and executes as a numeric query."""
from __future__ import annotations

import asyncio
import json
import sqlite3

import httpx
import pytest
from textual.widgets import Button, DataTable, TextArea

from d8r.engine import add_sqlite_source
from d8r.tui.app import D8RApp
from tests.test_ai_tui import completion, configured, finished, frame, install_provider


def test_ai_cents_conversion_can_be_applied_and_run_without_integer_truncation(tmp_path, monkeypatch):
    path = tmp_path / "money.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("create table payments (amount_cents integer)")
        connection.executemany("insert into payments values (?)", [(199,), (-50,), (None,)])
    source = add_sqlite_source("money", str(path))
    connection.close()
    original = "\\from payments\n\\select sum(amount_cents) as total_cents"
    converted = "\\from payments\n\\select sum(amount_cents) / 100 as total_dollars"
    requests = []

    def provider(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            call = {"index": 0, "id": "validate-conversion", "type": "function", "function": {
                "name": "validate_d8r", "arguments": json.dumps({"text": converted}),
            }}
            content = frame({"tool_calls": [call]}) + frame({}, "tool_calls") + b"data: [DONE]\n\n"
        else:
            validation = next(json.loads(message["content"]) for message in payload["messages"]
                              if message["role"] == "tool")
            assert validation["valid"] is True
            content = completion("```d8r\n" + converted + "\n```")
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=content)

    install_provider(monkeypatch, provider)

    async def scenario():
        session = configured()
        session.register(source)
        app = D8RApp(session)
        async with app.run_test(size=(140, 45)) as pilot:
            app.editor.load_text(original)
            app.action_ai()
            panel = app.ai_panel
            panel.query_one("#ai-input", TextArea).load_text("Convert total_cents to dollars.")
            panel.action_send()
            await finished(panel)
            await pilot.pause()
            assert not panel.query_one("#ai-apply", Button).disabled
            assert app.editor.text == original and not session.history
            panel.action_apply()
            assert app.editor.text == converted and not session.history
            panel.action_close()
            await pilot.pause()
            app.action_run()
            await app.workers.wait_for_complete()
            await pilot.pause()
            table = app.query_one("#results-table", DataTable)
            assert table.row_count == 1
            assert float(str(table.get_cell_at((0, 0)))) == pytest.approx(1.49)
            assert [entry.doc for entry in session.history] == [converted]
            assert not session.fns

    try:
        asyncio.run(scenario())
    finally:
        source.con.disconnect()
