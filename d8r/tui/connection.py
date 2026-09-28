"""Bounded, explicitly requested connections and their nonmodal progress UI."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from threading import Lock, Thread
from time import monotonic
from typing import TYPE_CHECKING

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, LoadingIndicator, Static

from d8r.engine import DataSource

if TYPE_CHECKING:
    from d8r.tui.session import Session


CONNECTION_TIMEOUT = 30.0


def release_source(source: DataSource) -> None:
    """Dispose an unregistered source, without exposing driver errors/secrets."""
    try:
        if source.d1 is not None:
            source.d1.close()
        elif source.con is not None:
            source.con.disconnect()
    except Exception:
        # Cleanup must not resurrect a cancelled operation or crash the UI.
        pass


async def build_connection(
    build: Callable[[], DataSource], *, timeout: float = CONNECTION_TIMEOUT,
) -> DataSource:
    """Transfer one built source, or close it even if its builder finishes late.

    Drivers cannot reliably interrupt connection/schema discovery. The daemon
    owns its result until the awaiting caller claims it; abandonment and handoff
    share a lock. Unlike a cancelled to_thread Task, this ownership survives
    event-loop shutdown, so late results still get closed in the builder thread.
    """
    if timeout <= 0:
        raise ValueError("Connection timeout must be positive.")
    lock = Lock()
    result: Future[DataSource] = Future()
    abandoned = False

    def construct() -> None:
        try:
            source = build()
        except Exception as exc:
            result.set_exception(exc)
            return
        with lock:
            discard = abandoned
            if not discard:
                result.set_result(source)
        if discard:
            release_source(source)

    pending = asyncio.wrap_future(result)
    # A timed-out caller no longer awaits this future. Retrieve a later driver
    # exception so it cannot leak credentials via an unhandled-task diagnostic.
    pending.add_done_callback(lambda future: None if future.cancelled() else future.exception())
    deadline = monotonic() + timeout
    Thread(target=construct, name="d8r-source-connect", daemon=True).start()
    try:
        source = await asyncio.wait_for(asyncio.shield(pending), timeout)
        if monotonic() >= deadline:
            raise TimeoutError
        return source
    except BaseException:
        with lock:
            abandoned = True
            finished = result.done() and result.exception() is None
            source = result.result() if finished else None
        if source is not None:
            release_source(source)
        raise


def build_saved_source(
    session: Session, *, profile: dict[str, str] | None = None,
    snapshot_path: str | None = None,
) -> DataSource:
    """Build only; callers own registration and stale-target checks."""
    if snapshot_path:
        return session.build_sqlite_source(snapshot_path)
    if profile is None:
        raise ValueError("Connection details are required.")
    if profile.get("kind") == "postgres-live":
        return session.build_postgres_source(
            host=profile["host"], port=int(profile["port"]), database=profile["database"],
            user=profile["user"], password=profile["password"], schema=profile["schema"],
            sslmode=profile["sslmode"], display=profile.get("display"),
        )
    return session.build_live_source(
        profile["account_id"], profile["api_token"], profile["database"], profile.get("display", ""),
    )


def saved_source_label(
    *, profile: dict[str, str] | None = None, snapshot_path: str | None = None,
) -> str:
    """A database label, never a DSN or credential-bearing exception."""
    if snapshot_path:
        return Path(snapshot_path).name
    if profile is None:
        return "data source"
    label = profile.get("database", "data source")
    for key in ("password", "api_token"):
        secret = profile.get(key)
        if secret:
            label = label.replace(secret, "[redacted]")
    return label


class ConnectionStatus(Vertical):
    """Compact blinking dots and cancel action, shared with the Add/Test form."""

    DEFAULT_CSS = """
    ConnectionStatus {
        width: 46;
        max-width: 100%;
        height: auto;
        border: round $accent;
        background: $surface;
        padding: 0 1;
    }
    ConnectionStatus .connection-heading { height: 2; }
    ConnectionStatus LoadingIndicator { width: 5; height: 2; }
    ConnectionStatus .connection-label { width: 1fr; height: 2; }
    ConnectionStatus Button { height: 1; min-width: 10; }
    ConnectionProgress { dock: bottom; }
    """

    def compose(self) -> ComposeResult:
        with Horizontal(classes="connection-heading"):
            yield LoadingIndicator()
            yield Static("", classes="connection-label", markup=False)
        yield Button("Cancel", classes="connection-cancel", compact=True)

    def start(self, target: str) -> None:
        self.query_one(".connection-label", Static).update(f"Connecting to\n{target}")
        self.display = True

class ConnectionProgress(ConnectionStatus):
    """Mount on the workspace screen after an explicit reconnect action.

    Success transfers ownership to ``connected`` synchronously, only if ``valid``
    still holds. No persistence, registration or query execution happens here.
    Cancel/supersede by calling ``cancel``; unmount cancels automatically.
    """

    def __init__(
        self, target: str, build: Callable[[], DataSource], connected: Callable[[DataSource], None],
        *, valid: Callable[[], bool], failed: Callable[[str], None],
        timeout: float = CONNECTION_TIMEOUT, **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._target = target
        self._build = build
        self._connected = connected
        self._valid = valid
        self._failed = failed
        self._timeout = timeout
        self._worker = None
        self._cancelled = False

    def on_mount(self) -> None:
        # Textual marks is_mounted only after Mount dispatch completes. Starting
        # the worker inside that dispatch can make its stale-widget guard exit
        # before either the connection or its timeout has started.
        self.call_later(self._start_connection)

    def _start_connection(self) -> None:
        if self._cancelled or not self.is_mounted or not self._valid():
            self.remove()
            return
        self.start(self._target)
        self._worker = self.run_worker(self._connect(), name="Reconnect source", exit_on_error=False)

    async def _connect(self) -> None:
        source = None
        try:
            if self._cancelled or not self.is_mounted or not self._valid():
                return
            source = await build_connection(self._build, timeout=self._timeout)
            if self._cancelled or not self.is_mounted or not self._valid():
                return
            transferred, source = source, None
            self._connected(transferred)
        except TimeoutError:
            if not self._cancelled and self.is_mounted and self._valid():
                self._failed(f"Connection timed out after {self._timeout:g}s. Retry to connect.")
        except asyncio.CancelledError:
            raise
        except Exception:
            if not self._cancelled and self.is_mounted and self._valid():
                self._failed("Could not connect. Check the connection details and access permissions, then retry.")
        finally:
            if source is not None:
                release_source(source)
            self._worker = None
            if self.is_mounted:
                self.remove()

    def cancel(self) -> None:
        self._cancelled = True
        if self._worker is not None:
            self._worker.cancel()
        if self.is_mounted:
            self.remove()

    @on(Button.Pressed, ".connection-cancel")
    def _cancel_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if not self._cancelled and self._valid():
            self._failed("Connection cancelled.")
        self.cancel()

    def on_unmount(self) -> None:
        self._cancelled = True
        if self._worker is not None:
            self._worker.cancel()
