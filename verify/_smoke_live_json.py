"""Throwaway live-provider/TUI smoke using synthetic JSON, never the user's workspace."""
import asyncio
import json
import sqlite3
import tempfile
from pathlib import Path

from textual.widgets import Static, TextArea

from d8r.engine import add_sqlite_source
from d8r.tui.app import D8RApp
from d8r.tui.results import ResultsTable
from d8r.tui.session import Session


async def main():
    configured = Session()
    config = configured.ai_config
    config.validate()
    with tempfile.TemporaryDirectory(prefix="d8r-live-json-") as folder:
        root = Path(folder)
        path = root / "synthetic.sqlite"
        documents = [
            {"event": "invoice.paid", "invoice": {"id": "in_demo_1", "amount_cents": 1999,
                                                   "currency": "usd", "paid": True},
             "items": [{"description": "Plan A"}]},
            {"event": "invoice.open", "invoice": {"id": "in_demo_2", "amount_cents": 0,
                                                   "currency": "eur", "paid": False}, "items": []},
            None,
        ]
        with sqlite3.connect(path) as connection:
            extra = ", ".join(f"padding_{i} text" for i in range(21))
            connection.execute(f"create table billing_events (id integer, {extra}, details_json text)")
            connection.executemany("insert into billing_events (id, details_json) values (?, ?)",
                                   [(i + 1, json.dumps(value) if value is not None else None)
                                    for i, value in enumerate(documents)])
        connection.close()
        source = add_sqlite_source("synthetic-billing", str(path))
        session = Session(data_dir=root / "state")
        session.ai_config = config
        session.register(source)
        app = D8RApp(session)
        try:
            async with app.run_test(size=(180, 55)) as pilot:
                app.editor.load_text("\\from billing_events")
                await pilot.pause()
                app.action_ai()
                await pilot.pause()
                panel = app.ai_panel
                prompt = (
                    "How, can you extract the data out of billing events details_json? "
                    "Make a query page extracting id, event, invoice.id as invoice_id, "
                    "invoice.amount_cents as amount_cents, invoice.currency as currency, "
                    "invoice.paid as paid, and items[0].description as item_description. "
                    "Inspect the actual JSON keys and preserve text/integer/boolean types. "
                    "Apply the query but do not run it."
                )
                panel.query_one("#ai-input", TextArea).load_text(prompt)
                panel.action_send()
                await asyncio.wait_for(app.workers.wait_for_complete(), 600)
                await pilot.pause()
                chat = app.ai_chats.chats[panel.chat_id]
                if chat.status == "error":
                    print(json.dumps({"live_provider": "error", "message": chat.status_text,
                                      "diagnostic": chat.diagnostics.text}, ensure_ascii=False))
                    raise AssertionError("Configured provider did not complete the synthetic JSON task")
                assert chat.context is not None and chat.context.applied_queries, chat.status_text
                assert not session.history, "AI must not execute its applied query"
                applied = app.editor.text
                print(json.dumps({"live_provider": "applied", "query": applied}, ensure_ascii=False), flush=True)
                panel.action_close()
                app.editor.focus()
                await pilot.pause()
                await pilot.press("ctrl+enter")
                await asyncio.wait_for(app.workers.wait_for_complete(), 60)
                await pilot.pause()
                table = app.query_one("#results-table", ResultsTable)
                print(json.dumps({"run_error": str(app.query_one("#results-error", Static).content),
                                  "run_busy": app.run_busy, "session_busy": session.busy,
                                  "columns": table.raw_columns, "rows": table.raw_rows}, ensure_ascii=False), flush=True)
                assert session.history and session.history[0].doc == applied
                columns = list(table.raw_columns)
                rows = {row[columns.index("id")]: dict(zip(columns, row)) for row in table.raw_rows}
                expected = {
                    "event": "invoice.paid", "invoice_id": "in_demo_1", "amount_cents": 1999,
                    "currency": "usd", "paid": True, "item_description": "Plan A",
                }
                assert all(rows[1][name] == value for name, value in expected.items()), rows
                assert type(rows[1]["amount_cents"]) is int
                assert rows[2]["paid"] is False and rows[2]["item_description"] is None, rows
                assert all(rows[3][name] is None for name in expected), rows
                assert table.row_count == 3
                print(json.dumps({"live_provider": "completed", "data": "synthetic only",
                                  "applied_query": applied, "columns": columns,
                                  "rows": table.raw_rows, "auto_executed": False,
                                  "explicit_run_verified": True}, ensure_ascii=False))
        finally:
            source.con.disconnect()


asyncio.run(main())
