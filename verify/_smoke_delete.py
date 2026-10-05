"""Throwaway Textual smoke for deletion, cancellation, and restart persistence."""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

import httpx
from textual.widgets import Button, OptionList, TextArea

from d8r.ai import client
from d8r.ai.client import AIConfig
from d8r.tui.app import D8RApp
from d8r.tui.session import Session


def delete_button(container):
    return next(button for button in container.query(Button) if "delete" in str(button.label).lower())


async def main():
    captures = Path(sys.argv[1])
    with tempfile.TemporaryDirectory(prefix="d8r-delete-") as folder:
        state = Path(folder) / "state"
        session = Session(data_dir=state)
        app = D8RApp(session)
        deleted_pages = set()
        deleted_chats = set()
        original_client = client.create_client
        opened = asyncio.Event()

        class Pending(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                opened.set()
                yield b'data: {"choices":[{"index":0,"delta":{"role":"assistant"}}]}\n\n'
                await asyncio.Event().wait()

            async def aclose(self):
                self.closed = True

        stream = Pending()
        client.create_client = lambda config: httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream)
        ))
        try:
            async with app.run_test(size=(150, 48)) as pilot:
                await pilot.pause()
                assert not app.ai_chats.chats, "Mount must not create empty durable chats"
                first = app._document_identity
                app.editor.load_text("\\from events\n\\limit 1")
                await pilot.pause(0.3)
                app.action_toggle_pages()
                await pilot.pause()
                assert delete_button(app.query_one("#tab-pages"))
                await pilot.click("#page-new")
                await pilot.pause()
                second = app._document_identity
                app.editor.load_text("\\from users\n\\limit 1")
                await pilot.pause(0.3)
                related = app.ai_chats.create(app._ai_target())
                related.input = "A synthetic draft for this page"
                app.ai_chats.persist(related)
                await pilot.click("#page-new")
                await pilot.pause()
                third = app._document_identity
                app.editor.load_text("\\from events\n\\limit 2")
                await pilot.pause(0.3)
                assert len(app.ai_chats.chats) == 1, "Page switches must not create empty chats"
                listing = app.query_one("#pages-list", OptionList)
                listing.highlighted = listing.get_option_index(second)
                listing.focus()
                await pilot.press("delete")
                await pilot.pause()
                deleted_pages.add(second)
                deleted_chats.add(related.id)
                assert app._document_identity == third
                assert app.editor.text == "\\from events\n\\limit 2"
                assert second not in {page["id"] for page in session.workspace["pages"]}
                assert related.id not in app.ai_chats.chats
                captures.joinpath("pages-delete.svg").write_text(app.export_screenshot(), encoding="utf-8")

                session.ai_config = AIConfig("https://provider.invalid/v1", "synthetic", max_attempts=1)
                app.action_ai()
                await pilot.pause()
                panel = app.ai_panel
                panel.query_one("#ai-input", TextArea).load_text("Create a synthetic query")
                panel.action_send()
                await asyncio.wait_for(opened.wait(), 3)
                running = app.ai_chats.chats[panel.chat_id]
                context = running.context
                assert context is not None
                app.action_agents()
                await pilot.pause()
                roster = app.query_one("#agents-list", OptionList)
                roster.highlighted = roster.get_option_index(running.id)
                roster.focus()
                delete = delete_button(app.query_one("#workspace-agents"))
                captures.joinpath("agents-delete.svg").write_text(app.export_screenshot(), encoding="utf-8")
                await pilot.click(delete)
                await pilot.pause()
                deleted_chats.add(running.id)
                assert running.id not in app.ai_chats.chats and running.id not in session.load_chats()
                assert stream.closed
                before = app.editor.text
                late = json.loads(await context.call_tool("apply_queries", {
                    "queries": [{"title": "Late result", "body": "\\from events\n\\limit 3"}],
                }))
                assert not late.get("applied")
                assert app.editor.text == before
                assert not app.ai_chats.chats

                app.action_toggle_pages()
                await pilot.pause()
                listing.highlighted = listing.get_option_index(third)
                listing.focus()
                await pilot.press("delete")
                await pilot.pause()
                deleted_pages.add(third)
                assert app._document_identity == first
                listing.highlighted = listing.get_option_index(first)
                listing.focus()
                await pilot.press("delete")
                await pilot.pause()
                deleted_pages.add(first)
                assert app._document_identity not in deleted_pages
                assert app.editor.text == ""
                assert len(session.workspace["pages"]) == 1
                assert not session.history
                app.exit()
        finally:
            client.create_client = original_client
        reopened = Session(data_dir=state)
        assert not deleted_pages.intersection(page["id"] for page in reopened.workspace["pages"])
        assert not deleted_chats.intersection(reopened.load_chats())
        assert not reopened.load_chats()
        print(json.dumps({"inactive_page_kept_editor": True, "page_chats_removed": True,
                          "running_chat_cancelled": True, "late_apply_rejected": True,
                          "active_and_last_page_deleted": True, "restart_did_not_restore_deleted_items": True,
                          "implicit_empty_chats": 0, "ui_captures": ["pages-delete.svg", "agents-delete.svg"]}))


asyncio.run(main())
