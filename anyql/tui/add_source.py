"""Connect to live Cloudflare D1, or open an explicitly selected SQLite snapshot.

Only Add remembers a live profile including its API token and transfers the source to the
app. Test owns its connection until Add, a field change, or cancellation.
"""

from __future__ import annotations

import asyncio

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static

from anyql.engine import D1Error, DataSource, PayloadError


class AddSourceModal(ModalScreen):
    """The credential form; network work never blocks Textual's event loop."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, session, *, profile: dict[str, str] | None = None, snapshot_path: str | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.session = session
        self._built: tuple[tuple[str, ...], DataSource] | None = None
        self._worker = None
        self._generation = 0
        self._connection_form_closed = False
        self._busy = False
        self._observed_fields: tuple[str, ...] = ()
        self._profiles = list(session.d1_profiles)
        self._initial_profile = dict(profile) if profile is not None else None
        self._snapshot_path = snapshot_path
        self._reconnect_fields: tuple[str, ...] | None = None

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="add-source"):
            yield Label("Reconnect local SQLite snapshot" if self._snapshot_path else
                        "Connect saved data source · Cloudflare D1" if self._initial_profile is not None else
                        "Add a data source · Cloudflare D1", id="add-source-title")
            yield Static(
                "Live connects to your hosted database through Cloudflare's official API. "
                "A local SQLite snapshot is offline and does not sync with Cloudflare.",
                id="add-source-note",
                markup=False,
            )
            yield Static(
                "Cloudflare dashboard: copy your Account ID from the account overview. "
                "Open Workers & Pages → D1 → your database for its UUID or database name "
                "(not a Worker binding).\n"
                "My Profile → API Tokens → Create Token → Custom token: choose "
                "Account → D1 → Read and include only the intended account. "
                "Use this scoped read-only token, not a Global API key.\n"
                "Add saves the Account ID, resolved database UUID, display name and API token. "
                "Connection backups contain plaintext secrets; keep them private. Test and Cancel save nothing.",
                id="d1-setup-help",
                markup=False,
            )
            yield Label("Saved connection", classes="field-label")
            yield Select(
                [(f"{profile['display']} · {profile['database']}", index)
                 for index, profile in enumerate(self._profiles)],
                prompt="New connection",
                id="d1-profile",
            )
            yield Label("Account ID", classes="field-label")
            yield Input(placeholder="32-character Cloudflare account id", id="account-id")
            yield Label("Database UUID or name", classes="field-label")
            yield Input(placeholder="UUID recommended; not a Worker binding", id="database")
            yield Label("API token · Account / D1 / Read", classes="field-label")
            yield Input(placeholder="saved locally on Add; keep backups private", password=True, id="api-token")
            yield Label("Local snapshot · optional, overrides the live fields", classes="field-label")
            yield Input(self._snapshot_path or "", placeholder="existing .sqlite path — skips the network", id="snapshot-path")
            yield Label("Display name", classes="field-label")
            yield Input(placeholder="optional label for the datasource list", id="display-name")
            yield Static("", id="add-source-message", markup=False)
            with Horizontal(id="add-source-buttons"):
                yield Button("Test connection", id="test")
                yield Button("Connect" if self._initial_profile is not None or self._snapshot_path else "Add", variant="primary", id="add")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self._observed_fields = self._fields()
        self.query_one("#account-id", Input).focus()
        if self._initial_profile is not None:
            with self.prevent(Select.Changed, Input.Changed):
                self.query_one("#d1-profile", Select).value = self._profiles.index(self._initial_profile)
                self._fill_profile(self._initial_profile)
            self._reconnect_fields = self._fields()
            if self._initial_profile.get("api_token"):
                self.call_after_refresh(self.action_add)
            else:
                self.query_one("#api-token", Input).focus()
        elif self._snapshot_path:
            self.query_one("#snapshot-path", Input).focus()

    def _fields(self) -> tuple[str, ...]:
        return tuple(self.query_one(f"#{name}", Input).value.strip() for name in (
            "account-id", "database", "api-token", "snapshot-path", "display-name",
        ))

    def _message(self, text: str, error: bool = False) -> None:
        token = self.query_one("#api-token", Input).value.strip()
        widget = self.query_one("#add-source-message", Static)
        widget.update(text.replace(token, "[redacted]") if token else text)
        widget.set_class(error, "error")

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for name in ("test", "add"):
            self.query_one(f"#{name}", Button).disabled = busy

    @staticmethod
    def _release(source: DataSource) -> None:
        """Release only sources still owned by this modal, never registered ones."""
        if source.d1 is not None:
            source.d1.close()
        elif source.con is not None:
            source.con.disconnect()

    @classmethod
    def _release_finished(cls, task: asyncio.Task) -> None:
        # to_thread cannot stop a running HTTP request. A cancelled screen must
        # still claim and close the eventual result, including the completion race.
        try:
            source = task.result()
        except (asyncio.CancelledError, Exception):
            return
        cls._release(source)

    def _invalidate(self) -> None:
        self._generation += 1
        if self._worker is not None:
            self._worker.cancel()
            self._worker = None
        if self._built is not None:
            self._release(self._built[1])
            self._built = None
        if not self._connection_form_closed:
            self._set_busy(False)

    @on(Input.Changed)
    def _fields_changed(self) -> None:
        if self._connection_form_closed:
            return
        fields = self._fields()
        if fields != self._observed_fields:
            self._observed_fields = fields
            self._invalidate()
            self._message("Fields changed. Test or Add to connect with these values.")

    @on(Select.Changed, "#d1-profile")
    def _profile_selected(self, event: Select.Changed) -> None:
        if self._connection_form_closed:
            return
        self._invalidate()
        profile = self._profiles[event.value] if isinstance(event.value, int) else {}
        self._fill_profile(profile)

    def _fill_profile(self, profile: dict[str, str]) -> None:
        for name, key in (("account-id", "account_id"), ("database", "database"), ("display-name", "display")):
            self.query_one(f"#{name}", Input).value = profile.get(key, "")
        self.query_one("#api-token", Input).value = profile.get("api_token", "")
        self.query_one("#snapshot-path", Input).value = ""
        self._observed_fields = self._fields()
        self._message(
            "Saved API token loaded. Test or Add to connect." if profile.get("api_token")
            else "Enter an API token for this saved connection." if profile else ""
        )

    def _build(self, fields: tuple[str, ...]) -> DataSource:
        account_id, database, api_token, path, display = fields
        if path:
            return self.session.build_sqlite_source(path, display)
        if not (account_id and database and api_token):
            raise ValueError("Account ID, database UUID/name and API token are required for a live source.")
        return self.session.build_live_source(account_id, api_token, database, display)

    def _begin(self, add: bool) -> None:
        if self._connection_form_closed or self._busy:
            return
        fields = self._fields()
        if fields != self._observed_fields:
            self._invalidate()
            self._observed_fields = fields
        self._set_busy(True)
        self._message("Connecting… You can edit fields or Cancel while this runs.")
        self._worker = self.run_worker(
            self._connect(fields, self._generation, add),
            name="D1 connection", group="d1-connect", exit_on_error=False,
        )

    async def _connect(self, fields: tuple[str, ...], generation: int, add: bool) -> None:
        try:
            if self._built is not None and self._built[0] == fields:
                source = self._built[1]
            else:
                task = asyncio.create_task(asyncio.to_thread(self._build, fields))
                try:
                    source = await asyncio.shield(task)
                except asyncio.CancelledError:
                    task.add_done_callback(self._release_finished)
                    raise
                if self._connection_form_closed or generation != self._generation or fields != self._fields():
                    self._release(source)
                    return
                self._built = (fields, source)
            if add:
                if source.kind == "d1-live":
                    profile = (source.d1.account_id, source.d1.database_uuid, source.display)
                    if fields[2] and any(fields[2] in value for value in profile):
                        raise ValueError("The Account ID, database and display name must not contain the API token.")
                    # Selecting an unchanged saved profile reconnects, not re-saves.
                    if fields != self._reconnect_fields:
                        self.session.remember_d1(source.d1.account_id, source.d1.database_uuid, source.display, api_token=fields[2])
                self._built = None  # ownership passes to the app's dismissal callback
                self._connection_form_closed = True
                self.dismiss(source)
            else:
                tables = ", ".join(source.datasets) or "no user tables"
                self._message(f"{source.display} · {len(source.datasets)} tables · {tables}")
        except asyncio.CancelledError:
            raise
        except (D1Error, PayloadError, OSError, RuntimeError, ValueError) as exc:
            if not self._connection_form_closed and generation == self._generation and fields == self._fields():
                self._message(str(exc), error=True)
        except Exception:
            if not self._connection_form_closed and generation == self._generation and fields == self._fields():
                self._message("Could not connect. Check the account, database and token permissions, then try again.", error=True)
        finally:
            if generation == self._generation:
                self._worker = None
                if not self._connection_form_closed:
                    self._set_busy(False)

    def action_cancel(self) -> None:
        self._connection_form_closed = True
        self._invalidate()
        self.query_one("#api-token", Input).value = ""
        self.dismiss(None)

    def on_unmount(self) -> None:
        self._connection_form_closed = True
        self._invalidate()

    def action_test(self) -> None:
        self._begin(add=False)

    def action_add(self) -> None:
        self._begin(add=True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "test":
            self.action_test()
        elif event.button.id == "add":
            self.action_add()
        elif event.button.id == "cancel":
            self.action_cancel()
