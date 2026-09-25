"""Target-scoped saved conversations and explicit, guarded replacement proposals."""

from __future__ import annotations

import asyncio
import json
import re
import traceback
from collections.abc import Callable
from dataclasses import dataclass

from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Button, Static, TextArea
from textual.worker import Worker
from textual.timer import Timer

from d8r.ai import client
from d8r.ai.context import AIContext, AIProposal
from d8r.ai.diagnostics import AIDiagnostics



class ComposerArea(TextArea):
    """The chat composer: Enter sends, Shift+Enter inserts a newline.

    Textual's `TextArea` inserts a newline on plain `enter` and ignores
    `shift+enter` — the wrong mapping for a one-shot message box. The two
    keys are claimed here, one level above the base handler (the same rule
    the document editor follows for its palette keys), so nothing else on
    the panel loses Enter: buttons still activate on it.
    """

    class Submitted(Message):
        """Enter asked to send the composer's text."""


    async def _on_key(self, event: events.Key) -> None:
        self._restart_blink()
        if self.read_only:
            return
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted())
        elif event.key == "shift+enter":
            event.stop()
            event.prevent_default()
            self.insert("\n")
        else:
            await super()._on_key(event)


class AIDiagnosticsScreen(ModalScreen):
    """A copyable snapshot; inspecting it never sends anything to the provider."""

    BINDINGS = [Binding("escape", "close", "Close logs")]

    def __init__(self, diagnostics: AIDiagnostics) -> None:
        super().__init__()
        self.diagnostics = diagnostics

    def compose(self) -> ComposeResult:
        with Vertical(id="ai-log-screen"):
            yield Static("AI diagnostics · current chat", classes="pane-title")
            yield Static("In memory only. May contain query text and data values; review before sharing.\n"
                         "Known AI/D1 keys are redacted. New chat or a source change clears these logs.\n"
                         "Validation is parser-only: a rejected operation may still be supported by Ibis.",
                         id="ai-log-notice", markup=False)
            yield TextArea("",
                           read_only=True, soft_wrap=True, id="ai-log-text")
            with Horizontal(id="ai-log-buttons"):
                yield Button("Copy logs", id="ai-log-copy")
                yield Button("Refresh", id="ai-log-refresh")
                yield Button("Close", id="ai-log-close")

    def on_mount(self) -> None:
        self._refresh_log()

    def _refresh_log(self) -> None:
        editor = self.query_one("#ai-log-text", TextArea)
        text = self.diagnostics.text or "No AI requests logged yet."
        editor.load_text(text)
        editor.move_cursor((text.count("\n"), 0))

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "ai-log-copy":
            self.app.copy_to_clipboard(self.query_one("#ai-log-text", TextArea).text)
            self.app.notify("Copied AI diagnostics. Review query/data values before sharing.")
        elif event.button.id == "ai-log-refresh":
            self._refresh_log()
        elif event.button.id == "ai-log-close":
            self.action_close()

    def action_close(self) -> None:
        self.dismiss()


@dataclass(frozen=True)
class AITarget:
    """A synchronous snapshot: identity isolates chats; all fields guard Apply."""

    identity: tuple
    source_id: str
    document: str
    parameters: tuple[str, ...] | None = None
    details: str = ""
    function_name: str = ""




class AIPanel(Vertical):
    """One mounted conversation; the owning editor supplies snapshot and Apply."""

    BINDINGS = [
        Binding("tab", "next_control", "Next control", show=False, priority=True),
        Binding("shift+tab", "previous_control", "Previous control", show=False, priority=True),
        Binding("ctrl+enter", "send", "Send", show=False, priority=True),
        Binding("escape", "close", "Close AI", show=False),
    ]

    def __init__(self, session, snapshot: Callable[[], AITarget],
                 apply: Callable[[AIProposal], None], return_focus: Callable[[], None],
                 function_mode: bool = False, **kwargs) -> None:
        super().__init__(**kwargs)
        self.session = session
        self.snapshot = snapshot
        self.apply_document = apply
        self.return_focus = return_focus
        self.function_mode = function_mode
        self.messages: list[dict] = []
        self._identity: tuple | None = None
        self._target: AITarget | None = None
        self._ai_context: AIContext | None = None
        self._proposal: AIProposal | None = None
        self._worker: Worker | None = None
        self._context_tasks: set[asyncio.Task] = set()
        self._generation = 0
        self._transcript = ""
        self._diagnostics = AIDiagnostics()
        self._turn = 0
        self._partial_answer = ""
        self._input = ""
        self._composer: TextArea | None = None
        self._pending_prompt = ""
        self._save_timer: Timer | None = None
        self._save_error = ""
        self._status_text = ""
        # Textual hides widgets during teardown; persist the user's choice,
        # not that transient DOM state.
        self._chat_visible = False
        self.display = False

    def compose(self) -> ComposeResult:
        yield Static("Make a function with AI" if self.function_mode else
                     "AI assistant · review before Apply", classes="pane-title")
        if self.function_mode:
            yield Static("Describe what you want → review the draft → Apply to editor.\n"
                         "No need to fill in the form first. You can ask for changes before applying.",
                         classes="ai-notice", id="ai-help", markup=False)
        yield Static(
            "Document, schema, samples, functions and history may be sent to your configured provider.",
            classes="ai-notice", markup=False,
        )
        review = VerticalScroll if self.function_mode else Vertical
        with review(id="ai-review"):
            with VerticalScroll(id="ai-conversation"):
                yield Static("", id="ai-transcript", markup=False)
            yield Static("", id="ai-status", markup=False)
            yield Static("", id="ai-function-details", markup=False)
            yield TextArea("", read_only=True, show_line_numbers=True, id="ai-proposal")
        composer = ComposerArea(
            placeholder="What should this function return?" if self.function_mode else "Ask about this document…",
            soft_wrap=True, highlight_cursor_line=False, id="ai-input",
        )
        composer.border_title = "Message"
        composer.border_subtitle = "Shift+Enter: newline · Enter: send"
        yield composer
        with Horizontal(classes="ai-buttons"):
            yield Button("Generate" if self.function_mode else "Send", id="ai-send", variant="primary")
            yield Button("Cancel", id="ai-cancel", disabled=True)
            yield Button("Apply", id="ai-apply", disabled=True, variant="success")
            if self.function_mode:
                yield Button("Start over", id="ai-clear")
                yield Button("Logs", id="ai-logs")
                yield Button("Settings", id="ai-settings")
                yield Button("Back", id="ai-close")
        if not self.function_mode:
            with Horizontal(classes="ai-buttons"):
                yield Button("New chat", id="ai-clear")
                yield Button("AI settings", id="ai-settings")
                yield Button("Logs", id="ai-logs")
                yield Button("Close", id="ai-close")

    def on_mount(self) -> None:
        self._composer = self.query_one("#ai-input", TextArea)
        self.query_one("#ai-proposal").display = False
        self.query_one("#ai-function-details").display = False
        self.target_changed()

    @property
    def busy(self) -> bool:
        return self._worker is not None


    def open(self) -> None:
        self.target_changed()
        self._chat_visible = self.display = True
        self._flush_chat()
        self.query_one("#ai-input", TextArea).focus()
        if not self.session.ai_config.base_url or not self.session.ai_config.model:
            self._status("Set up your provider with AI settings, then describe what you want.")
        elif not self.messages and not self.busy:
            self._status("Describe the result and any inputs, then press Enter to generate." if self.function_mode else
                         "Ask a question or request a full replacement. Nothing is applied automatically.")

    def _status(self, text: str) -> None:
        self._status_text = text
        try:
            self.query_one("#ai-status", Static).update(
                text + ("\n" + self._save_error if self._save_error else "")
            )
        except NoMatches:
            pass  # Unmount may have already removed the children.

    def _chat_key(self) -> str | None:
        if self._identity is None:
            return None
        return json.dumps(["function" if self.function_mode else "workspace", *self._identity])

    def _capture_input(self) -> None:
        if self._composer is not None:
            # Changed may still be queued and children already unmounted when
            # the app exits. The retained editor holds the actual latest text.
            self._input = self._composer.text

    def _set_input(self, text: str) -> None:
        self._input = text
        self.query_one("#ai-input", TextArea).load_text(text)

    def _stop_save_timer(self) -> None:
        if self._save_timer is not None:
            self._save_timer.stop()
            self._save_timer = None

    def _persist(self, chat: dict | None) -> None:
        key = self._chat_key()
        if key is None:
            return
        try:
            self.session.save_chat(key, chat)
        except ValueError:
            # Store failures retain the in-memory draft; never echo paths/secrets
            # or turn a completed provider response into a request failure.
            error = "AI chat could not be saved. Your draft remains available in this session."
            if not self._save_error and self.is_mounted:
                self.app.notify(error, severity="error")
            self._save_error = error
        else:
            self._save_error = ""
        self._status(self._status_text)

    def _flush_chat(self) -> None:
        self._stop_save_timer()
        self._capture_input()
        self._persist({
            "messages": self.messages,
            "transcript": self._transcript,
            "input": self._input or self._pending_prompt,
            "turns": sum(message["role"] == "user" for message in self.messages),
            "visible": self._chat_visible,
        })

    @on(TextArea.Changed, "#ai-input")
    def _composer_changed(self, event: TextArea.Changed) -> None:
        text = event.text_area.text
        if text == self._input:
            return
        self._input = text
        self._stop_save_timer()
        self._save_timer = self.set_timer(0.3, self._flush_chat)

    def _draw_transcript(self, text: str) -> None:
        self.query_one("#ai-transcript", Static).update(text)
        self.query_one("#ai-conversation", VerticalScroll).scroll_end(animate=False)

    def _clear_proposal(self) -> None:
        self._proposal = None
        self.query_one("#ai-function-details").display = False
        self.query_one("#ai-apply", Button).disabled = True
        proposal = self.query_one("#ai-proposal", TextArea)
        proposal.load_text("")
        proposal.display = False

    def _set_busy(self, busy: bool) -> None:
        self.query_one("#ai-send", Button).disabled = busy
        self.query_one("#ai-cancel", Button).disabled = not busy
        self.query_one("#ai-settings", Button).disabled = busy
        if self.function_mode:
            self.query_one("#ai-send", Button).label = "Working…" if busy else "Generate"

    def target_changed(self) -> None:
        """Called by editors on changes, and checked again at every boundary."""
        current = self.snapshot()
        if current.identity != self._identity:
            self._flush_chat()
            self._reset_chat()
            self._identity = current.identity
            chat = self.session.load_chat(self._chat_key())
            if chat is not None:
                self.messages = chat["messages"]
                self._transcript = chat["transcript"]
                self._set_input(chat["input"])
                self._turn = chat["turns"]
                self._chat_visible = self.display = chat["visible"]
                self._draw_transcript(self._transcript)
                if self.function_mode:
                    self.query_one("#ai-help").display = not bool(self.messages)
                self._status("Conversation restored. Send again for a fresh proposal; nothing was applied or run.")
            else:
                self.display = self._chat_visible
                self._status("New target · start a conversation.")
        elif self._target is not None and current != self._target:
            self.action_cancel()
            self._clear_proposal()
            self._target = None
            self._status("Target changed. Send again to generate a fresh proposal.")

    def action_next_control(self) -> None:
        self._focus_control(1)

    def action_previous_control(self) -> None:
        self._focus_control(-1)

    def _focus_control(self, direction: int) -> None:
        controls = [self.query_one(f"#ai-{name}") for name in
                    ("proposal", "input", "send", "cancel", "apply", "clear", "settings", "logs", "close")]
        controls = [widget for widget in controls if widget.display and not widget.disabled]
        focused = self.screen.focused
        index = controls.index(focused) if focused in controls else -1
        controls[(index + direction) % len(controls)].focus()

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        actions = {
            "ai-send": self.action_send,
            "ai-cancel": self.action_cancel,
            "ai-apply": self.action_apply,
            "ai-clear": self.action_clear,
            "ai-close": self.action_close,
            "ai-settings": self.action_settings,
            "ai-logs": self.action_logs,
        }
        action = actions.get(event.button.id)
        if action is not None:
            event.stop()
            action()

    @on(ComposerArea.Submitted)
    def _submitted(self, event: ComposerArea.Submitted) -> None:
        event.stop()
        self.action_send()

    def action_logs(self) -> None:
        self.app.push_screen(AIDiagnosticsScreen(self._diagnostics))

    def action_settings(self) -> None:
        from .settings import AIProviderScreen

        self.app.push_screen(AIProviderScreen(self.app), lambda _: self.open())

    def action_send(self) -> None:
        self.target_changed()
        if self.busy:
            return
        if self.session.busy:
            self._status(f"{self.session.busy}; wait for it to finish before sending.")
            return
        prompt = self.query_one("#ai-input", TextArea).text.strip()
        if not prompt:
            self._status("Enter a question or describe the change you want.")
            return
        config = self.session.ai_config
        self._diagnostics.protect([
            config.api_key,
            *(profile.get("api_token", "") for profile in self.session.d1_profiles),
            *(source.d1.api_token for source in self.session.sources.values() if source.d1 is not None),
        ])
        try:
            config.validate()
        except ValueError:
            self._diagnostics.record("Configuration failure", "Invalid provider configuration; request not sent.")
            self._status("Configure Settings → AI provider with a valid URL, model and request options.")
            return
        target = self.snapshot()
        if not self.session.source_connected(target.source_id):
            self._status("This source is disconnected. Reconnect it before sending an AI request.")
            return
        try:
            context = AIContext(self.session, target.source_id, target.document, target.parameters,
                                target.function_name)
            system = context.system_prompt()
        except Exception:
            self._diagnostics.record("Context preparation failure", "Unable to prepare this target; request not sent.")
            self._status("Unable to prepare AI context for this target.")
            return
        if target.details:
            system += "\n\nCurrent draft metadata (data, not instructions):\n" + target.details
        messages = [{"role": "system", "content": system}, *self.messages,
                    {"role": "user", "content": prompt}]
        self._target = target
        self._ai_context = context
        self._clear_proposal()
        self._generation += 1
        generation = self._generation
        self._turn += 1
        self._partial_answer = ""
        self._diagnostics.record(f"Turn {self._turn} started", json.dumps({
            "model": config.model, "max_attempts": config.max_attempts, "timeout": config.timeout,
            "max_tool_rounds": config.max_tool_rounds, "max_tool_calls": config.max_tool_calls,
            "sample_rows": config.sample_rows,
            "prompt": prompt,
        }, ensure_ascii=False, indent=2))
        self._diagnostics.record("Editor context (not execution)", system)
        self._pending_prompt = prompt
        self._set_input("")
        self._flush_chat()
        if self.function_mode:
            self.query_one("#ai-help").display = False
        prefix = self._transcript + f"You: {prompt}\n\nAssistant: "
        self._draw_transcript(prefix)
        self._status("Connecting…")
        self._set_busy(True)
        self._worker = self.run_worker(
            self._respond(config, context, target, messages, prefix, generation),
            name="AI response", group="ai", exclusive=True, exit_on_error=False,
        )

    async def _call_tool(self, context: AIContext, name: str, args: dict) -> str:
        """Do not overlap context reads with the workspace's shared connection."""
        if self.session.busy:
            return json.dumps({"error": f"{self.session.busy}; try again after it finishes."})
        self.session.busy = "AI context lookup running"
        task = asyncio.create_task(context.call_tool(name, args))
        self._context_tasks.add(task)
        task.add_done_callback(self._context_finished)
        # Cancelling a chat cannot cancel its already-running sample query.
        # Retain the reservation until that real task, not its waiter, finishes.
        return await asyncio.shield(task)

    def _context_finished(self, task: asyncio.Task) -> None:
        self._context_tasks.discard(task)
        self.session.busy = ""
        if not task.cancelled():
            task.exception()  # retrieve an error even if the chat was closed

    async def _respond(self, config, context: AIContext, target: AITarget,
                       messages: list[dict], prefix: str, generation: int) -> None:
        answer = ""
        try:
            async for event in client.run_turn(
                config, messages, context.tools,
                lambda name, args: self._call_tool(context, name, args),
            ):
                if generation != self._generation:
                    return
                if self.snapshot() != target:
                    self.target_changed()
                    return
                if event.kind == "text":
                    answer += event.text
                    self._partial_answer = answer
                    if not self.function_mode:
                        self._draw_transcript(prefix + answer)
                    else:
                        self._status("Drafting your function…")
                elif event.kind == "status":
                    self._diagnostics.record("Status", event.text)
                    self._status(event.text)
                elif event.kind == "diagnostic":
                    self._diagnostics.record("Provider / tools", event.text)
            if generation != self._generation:
                return
            if self.snapshot() != target:
                self.target_changed()
                return
            self.messages = messages[1:]
            answer = messages[-1].get("content") or ""
            self._pending_prompt = ""
            self._partial_answer = ""
            self._diagnostics.record("Turn completed", "Complete provider response received; no query executed.")
            try:
                proposal = context.read_proposal(answer)
            except ValueError as exc:
                self._diagnostics.record("Proposal decoding failure", str(exc))
                self._transcript = prefix + answer + "\n\n"
                self._draw_transcript(self._transcript)
                self._status(str(exc))
                return
            if proposal is None:
                self._transcript = prefix + answer + "\n\n"
                self._draw_transcript(self._transcript)
                self._status("Reply below to continue." if self.function_mode else "Response complete.")
                return
            summary = re.sub(r"^```(?:d8r|json)[^\S\r\n]*\r?\n.*?^```[^\S\r\n]*$", "", answer,
                             flags=re.MULTILINE | re.DOTALL | re.IGNORECASE).strip() if self.function_mode else answer
            self._transcript = prefix + (summary or "Function draft ready for review.") + "\n\n"
            self._draw_transcript(self._transcript)
            pending_validation = bool(self.session.busy)
            error = None if pending_validation else context.validate_replacement(proposal)
            self._diagnostics.record("Proposal validation (parser only)",
                                     "Deferred: connection busy." if pending_validation else
                                     error or "Valid syntax; engine execution and dtype compatibility not checked.")
            self._proposal = proposal
            editor = self.query_one("#ai-proposal", TextArea)
            editor.load_text(proposal.body)
            editor.display = True
            if self.function_mode:
                editor.styles.height = min(20, max(4, len(proposal.body.splitlines()) + 2))
            if proposal.function is not None:
                fn = proposal.function
                details = self.query_one("#ai-function-details", Static)
                details.update(f"{fn.name}({', '.join(fn.params)})\n{fn.doc}\n"
                               f"Example call: {fn.name}({proposal.arguments})")
                details.display = True
            self.query_one("#ai-apply", Button).disabled = error is not None
            self._status(f"{self.session.busy}; wait before applying." if pending_validation else
                         "Needs a fix: " + error if error is not None else
                         "Review the draft, then Apply to editor. Or describe a change below. Nothing is saved or run.")
            if self.function_mode:
                self.query_one("#ai-input", TextArea).placeholder = "Describe a change, or ask a question…"
                if error is None:
                    self.call_after_refresh(self.query_one("#ai-review", VerticalScroll).scroll_end, animate=False)
        except asyncio.CancelledError:
            raise
        except client.AIError as exc:
            if generation == self._generation:
                self._diagnostics.record("Turn failed", str(exc))
                self._clear_proposal()
                self._draw_transcript(prefix + answer + "\n[Request failed; not added to chat history.]\n\n")
                self._status(str(exc) + " Open Logs for the diagnostic trace.")
        except Exception as exc:
            if generation == self._generation:
                locations = "\n".join(f"{frame.f_code.co_filename}:{line} in {frame.f_code.co_name}"
                                      for frame, line in traceback.walk_tb(exc.__traceback__))
                self._diagnostics.record("Internal failure", type(exc).__name__
                                         + ": exception text/locals withheld to protect secrets.\n" + locations)
                self._clear_proposal()
                self._status("The AI request could not complete. Open Logs for the diagnostic trace.")
        finally:
            if generation == self._generation:
                self._worker = None
                self._set_busy(False)
                if self._pending_prompt:
                    self._capture_input()
                    if not self._input:
                        self._set_input(self._pending_prompt)
                    self._pending_prompt = ""
                self._flush_chat()

    def action_apply(self) -> None:
        self.target_changed()
        if self.busy or self._proposal is None or self._target is None or self._ai_context is None:
            return
        if self.session.busy:
            self._status(f"{self.session.busy}; wait for it to finish before applying.")
            return
        try:
            error = self._ai_context.validate_replacement(self._proposal)
        except Exception:
            error = "Unable to validate this replacement. Send again."
        if error is not None:
            self._diagnostics.record("Apply rejected (parser only)", error)
            self.query_one("#ai-apply", Button).disabled = True
            self._status("Proposal cannot be applied: " + error)
            return
        proposal = self._proposal
        self._target = None
        self._clear_proposal()
        self.apply_document(proposal)
        self._diagnostics.record("Applied", "Replacement copied to editor; nothing executed or saved.")
        self._status("Applied to the editor only. Run or Save remains your choice.")
        if self.function_mode:
            self.action_close()

    def action_cancel(self) -> None:
        if self._worker is not None:
            if self._partial_answer:
                self._diagnostics.record("Incomplete assistant text (not applicable)", self._partial_answer)
            self._diagnostics.record("Turn cancelled", "Incomplete response discarded; document unchanged.")
            self._partial_answer = ""
            self._generation += 1
            self._worker.cancel()
            self._worker = None
            self._set_busy(False)
            self._clear_proposal()
            self._draw_transcript(self._transcript)
            self._capture_input()
            if not self._input:
                self._set_input(self._pending_prompt)
            self._pending_prompt = ""
            self._flush_chat()
            self._status("Cancelled. Incomplete response discarded; document unchanged.")

    def _reset_chat(self) -> None:
        self.action_cancel()
        self._stop_save_timer()
        self.messages = []
        self._transcript = ""
        self._diagnostics.clear()
        self._turn = 0
        self._partial_answer = ""
        self._pending_prompt = ""
        self._target = None
        self._ai_context = None
        self._set_input("")
        self._clear_proposal()
        self._draw_transcript("")
        if self.function_mode:
            self.query_one("#ai-help").display = True
            self.query_one("#ai-input", TextArea).placeholder = "What should this function return?"

    def action_clear(self) -> None:
        self._reset_chat()
        self._persist(None)
        self._status("New chat. Nothing has been applied.")

    def action_close(self) -> None:
        self.action_cancel()
        self._chat_visible = self.display = False
        self._flush_chat()
        self.return_focus()

    def on_unmount(self) -> None:
        self._flush_chat()
        if self._worker is not None:
            self._generation += 1
            self._worker.cancel()
            self._worker = None
