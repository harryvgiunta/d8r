"""Metadata generation commits only complete responses to unchanged pages."""
import asyncio
import json

import httpx
import pytest
from textual.widgets import Input, TextArea
from textual.worker import WorkerCancelled

from d8r.ai import client
from d8r.ai.page_details import generate_details
from d8r.tui.app import D8RApp
from d8r.tui.session import Session
from tests.test_ai_client import CONFIG, completed, stream_response


def test_details_generation_saves_metadata_without_changing_or_running_query(tmp_path, monkeypatch):
    def respond(request):
        payload = json.loads(request.content)
        assert "tools" not in payload
        assert json.loads(payload["messages"][-1]["content"])["document"] == "\\from events\n\\limit 3"
        return stream_response(completed(json.dumps({"title": "Event preview", "description": "The first three events."})))

    monkeypatch.setattr(client, "create_client", lambda config: httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    session = Session(data_dir=tmp_path)
    session.ai_config = CONFIG
    app = D8RApp(session)

    async def scenario():
        async with app.run_test(size=(150, 48)) as pilot:
            await pilot.pause()
            app.editor.load_text("\\from events\n\\limit 3")
            await pilot.click("#page-details-toggle")
            await pilot.click("#page-details-generate")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.query_one("#page-name", Input).value == "Event preview"
            assert app.query_one("#page-description", TextArea).text == "The first three events."
            assert app.editor.text == "\\from events\n\\limit 3"
            assert not session.history
            # A page switch before debounced messages dispatch must keep both fields.
            app.query_one("#page-description", TextArea).load_text("Edited description")
            first_id = app._document_identity
            app._new_page()
            app.open_page(first_id)
            assert app.query_one("#page-description", TextArea).text == "Edited description"
            app.exit()

    asyncio.run(scenario())
    page = next(page for page in Session(data_dir=tmp_path).workspace["pages"] if page["title"] == "Event preview")
    assert page["description"] == "Edited description"


@pytest.mark.parametrize("interrupt", ["edit", "switch", "cancel"])
def test_inflight_metadata_never_overwrites_changed_or_cancelled_target(tmp_path, monkeypatch, interrupt):
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        async def respond(request):
            started.set()
            await release.wait()
            return stream_response(completed(json.dumps({"title": "Late title", "description": "Late description"})))

        monkeypatch.setattr(client, "create_client", lambda config: httpx.AsyncClient(transport=httpx.MockTransport(respond)))
        session = Session(data_dir=tmp_path)
        session.ai_config = CONFIG
        app = D8RApp(session)
        async with app.run_test(size=(150, 48)) as pilot:
            await pilot.pause()
            await pilot.click("#page-details-toggle")
            app.query_one("#page-name", Input).value = "Keep this"
            await pilot.click("#page-details-generate")
            await asyncio.wait_for(started.wait(), 3)
            if interrupt == "edit":
                app.query_one("#page-name", Input).value = "My edit"
            elif interrupt == "switch":
                app._new_page()
            else:
                await pilot.click("#page-details-cancel")
            release.set()
            try:
                await app.workers.wait_for_complete()
            except WorkerCancelled:
                assert interrupt in {"switch", "cancel"}
            await pilot.pause()
            app._flush_workspace()
            assert all(page["title"] != "Late title" for page in session.workspace["pages"])
            expected = {"edit": "My edit", "switch": "", "cancel": "Keep this"}[interrupt]
            assert app.query_one("#page-name", Input).value == expected
            assert not session.history

    asyncio.run(scenario())


def test_invalid_metadata_response_is_rejected_atomically(monkeypatch):
    monkeypatch.setattr(client, "create_client", lambda config: httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: stream_response(completed(
            json.dumps({"title": "Valid title", "description": ["not a string"]}))))))
    with pytest.raises(client.AIError, match="invalid page details"):
        asyncio.run(generate_details(CONFIG, "\\from events"))
