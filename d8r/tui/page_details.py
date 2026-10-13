"""Collapsible metadata for the open query page; generation never executes it."""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Input, Static, TextArea
from textual.worker import Worker, get_current_worker

from d8r.ai.client import AIError
from d8r.ai.page_details import generate_details

if TYPE_CHECKING:
    from .app import D8RApp


class PageDetails(Vertical):
    def __init__(self, ide: D8RApp, **kwargs) -> None:
        super().__init__(**kwargs)
        self.ide = ide
        self.page_id = ""
        self.worker: Worker | None = None
        self._saved: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        with Horizontal(id="page-details-heading"):
            yield Static("Page Details", classes="pane-title")
            yield Button("Generate with AI", id="page-details-generate", compact=True,
                         tooltip="Send this query to the configured AI provider to name and describe it; never runs it")
            yield Button("Cancel", id="page-details-cancel", compact=True)
        yield Input(placeholder="Page name", id="page-name")
        yield TextArea("", id="page-description", soft_wrap=True)
        yield Static("Name and description autosave. AI receives only the query text.",
                     id="page-details-status", markup=False)

    def on_mount(self) -> None:
        self.query_one("#page-description", TextArea).border_title = "Description"
        self.query_one("#page-details-cancel", Button).display = False

    def sync(self) -> None:
        """Switch fields only with the open page, never a roster highlight."""
        page_id = self.ide._document_identity
        page = next((page for page in self.ide.session.workspace["pages"]
                     if page["id"] == page_id), {})
        saved = {"title": page.get("title", ""), "description": page.get("description", "")}
        switched = self.page_id != page_id
        if switched:
            self.cancel_generation()
            self.page_id = page_id
            self._status("Name and description autosave. AI receives only the query text.")
        if switched or saved != self._saved:
            if self.values() != saved:
                self._fill(saved["title"], saved["description"])
            self._saved = saved

    def _fill(self, title: str, description: str) -> None:
        with self.prevent(Input.Changed, TextArea.Changed):
            self.query_one("#page-name", Input).value = title
            self.query_one("#page-description", TextArea).load_text(description)

    def values(self) -> dict[str, str]:
        return {"title": self.query_one("#page-name", Input).value,
                "description": self.query_one("#page-description", TextArea).text}

    @on(Input.Changed, "#page-name")
    @on(TextArea.Changed, "#page-description")
    def _changed(self) -> None:
        self.ide._queue_workspace_save()

    def _status(self, message: str) -> None:
        self.query_one("#page-details-status", Static).update(message)

    def _target(self) -> tuple:
        return (self.ide._document_identity, self.ide.session.source_key(),
                self.ide.editor.text, self.values())

    @on(Button.Pressed, "#page-details-generate")
    def generate(self) -> None:
        if self.worker is not None:
            return
        try:
            self.ide.session.ai_config.validate()
        except ValueError:
            self._status("Configure Settings → AI provider before generating details.")
            return
        self.ide._flush_workspace()
        target = self._target()
        self._status("Generating page details…")
        self.query_one("#page-details-generate", Button).disabled = True
        self.query_one("#page-details-cancel", Button).display = True
        self.worker = self.run_worker(self._generate(target), exit_on_error=False)

    async def _generate(self, target: tuple) -> None:
        try:
            title, description = await generate_details(self.ide.session.ai_config, target[2])
            if self.ide._closing or not self.is_mounted:
                return
            if self.ide.function_mode or self._target() != target:
                self._status("Page, query or details changed; generated details discarded. Generate again.")
                return
            self._fill(title, description)
            self.ide._flush_workspace()
            self._status("Details generated and saved; query unchanged." if not self.ide._autosave_error
                         else "Details generated but not saved; resolve the workspace save error.")
        except asyncio.CancelledError:
            raise
        except AIError as exc:
            self._status(str(exc))
        except Exception:
            self._status("Unable to generate page details; existing details were kept.")
        finally:
            # A cancelled worker must not reset a newer request's controls.
            if self.worker is get_current_worker():
                self.worker = None
                if self.is_mounted:
                    self.query_one("#page-details-generate", Button).disabled = False
                    self.query_one("#page-details-cancel", Button).display = False

    @on(Button.Pressed, "#page-details-cancel")
    def cancel_generation(self) -> None:
        if self.worker is not None:
            self.worker.cancel()
            self.worker = None
        if self.is_mounted:
            self.query_one("#page-details-generate", Button).disabled = False
            self.query_one("#page-details-cancel", Button).display = False
            self._status("Generation cancelled; existing details kept.")
