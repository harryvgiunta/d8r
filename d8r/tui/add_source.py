"""Connect to live Cloudflare D1 or PostgreSQL, or open a SQLite snapshot.

Only Add remembers live credentials and transfers the source to the app.
Test owns its connection until Add, a field change, or cancellation.
"""

from __future__ import annotations

import asyncio

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static

from d8r.engine import D1Error, DataSource, PayloadError
from d8r.tui.connection import CONNECTION_TIMEOUT, ConnectionStatus, build_connection, release_source


class AddSourceModal(ModalScreen):
    """The credential form; network work never blocks Textual's event loop."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    CONNECTION_TIMEOUT = CONNECTION_TIMEOUT

    def __init__(self, session, *, profile: dict[str, str] | None = None, snapshot_path: str | None = None,
                 auto_connect: bool = False, **kwargs) -> None:
        super().__init__(**kwargs)
        self.session = session
        self._built: tuple[tuple[str, ...], DataSource] | None = None
        self._worker = None
        self._generation = 0
        self._connection_form_closed = False
        self._busy = False
        self._observed_fields: tuple[str, ...] = ()
        self._profiles = list(session.d1_profiles)
        self._postgres_profiles = list(session.postgres_profiles)
        self._initial_profile = dict(profile) if profile is not None else None
        self._snapshot_path = snapshot_path
        self._auto_connect = auto_connect
        self._reconnect_fields: tuple[str, ...] | None = None
        self._shown_backend: str | None = None

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="add-source"):
            yield Label("Reconnect local SQLite snapshot" if self._snapshot_path else
                        "Connect saved data source" if self._initial_profile is not None else
                        "Add a data source", id="add-source-title")
            yield Static(
                "Connect to Cloudflare D1 or PostgreSQL, or open an offline SQLite snapshot. "
                "Add saves connection credentials locally in plaintext; keep connection backups private. "
                "Test and Cancel save nothing.",
                id="add-source-note",
                markup=False,
            )
            yield Label("Backend", classes="field-label")
            yield Select(
                [("Cloudflare D1 / local SQLite snapshot", "d1"), ("PostgreSQL", "postgres")],
                value="postgres" if self._initial_profile and self._initial_profile.get("kind") == "postgres-live" else "d1",
                allow_blank=False,
                id="source-backend",
            )
            with Vertical(id="d1-fields"):
                yield Static(
                    "Cloudflare dashboard: copy your Account ID from the account overview. "
                    "Open Workers & Pages → D1 → your database for its UUID or database name "
                    "(not a Worker binding).\n"
                    "My Profile → API Tokens → Create Token → Custom token: choose "
                    "Account → D1 → Read and include only the intended account. "
                    "Use this scoped read-only token, not a Global API key.\n"
                    "A local SQLite snapshot is offline and does not sync with Cloudflare.",
                    id="d1-setup-help",
                    markup=False,
                )
                yield Label("Saved D1 connection", classes="field-label")
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
            with Vertical(id="postgres-fields"):
                yield Static(
                    "Use a database role with read-only access. For remote databases, use the TLS "
                    "mode required by your provider; verify-full verifies the server certificate and hostname.",
                    id="postgres-setup-help",
                    markup=False,
                )
                yield Label("Saved PostgreSQL connection", classes="field-label")
                yield Select(
                    [(f"{profile['display']} · {profile['database']}", index)
                     for index, profile in enumerate(self._postgres_profiles)],
                    prompt="New connection",
                    id="postgres-profile",
                )
                yield Label("Host", classes="field-label")
                yield Input("localhost", id="postgres-host")
                yield Label("Port", classes="field-label")
                yield Input("5432", type="integer", id="postgres-port")
                yield Label("Database", classes="field-label")
                yield Input(placeholder="database name", id="postgres-database")
                yield Label("User", classes="field-label")
                yield Input(placeholder="database role", id="postgres-user")
                yield Label("Password", classes="field-label")
                yield Input(placeholder="saved locally on Add; keep backups private", password=True, id="postgres-password")
                yield Label("Schema", classes="field-label")
                yield Input("public", id="postgres-schema")
                yield Label("TLS / SSL mode", classes="field-label")
                yield Select(
                    [(mode, mode) for mode in ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")],
                    value="prefer",
                    allow_blank=False,
                    id="postgres-sslmode",
                )
            yield Label("Display name", classes="field-label")
            yield Input(placeholder="optional label for the datasource list", id="display-name")
            yield Static("", id="add-source-message", markup=False)
            with Horizontal(id="add-source-buttons"):
                yield Button("Test connection", id="test")
                yield Button("Connect" if self._initial_profile is not None or self._snapshot_path else "Add", variant="primary", id="add")
                yield Button("Cancel", id="cancel")
        progress = ConnectionStatus(id="source-connection-progress")
        progress.display = False
        yield progress

    def on_mount(self) -> None:
        self._show_backend()
        self._observed_fields = self._fields()
        if self._initial_profile is not None:
            postgres = self._initial_profile.get("kind") == "postgres-live"
            profile = {key: value for key, value in self._initial_profile.items() if key != "kind"}
            profiles = self._postgres_profiles if postgres else self._profiles
            selector = "postgres-profile" if postgres else "d1-profile"
            with self.prevent(Select.Changed, Input.Changed):
                if profile in profiles:
                    self.query_one(f"#{selector}", Select).value = profiles.index(profile)
                if postgres:
                    self._fill_postgres_profile(profile)
                else:
                    self._fill_profile(profile)
            self._reconnect_fields = self._fields()
            if postgres or profile.get("api_token"):
                self.call_after_refresh(self.action_add)
            else:
                self.query_one("#api-token", Input).focus()
        elif self._snapshot_path:
            self.query_one("#snapshot-path", Input).focus()
            if self._auto_connect:
                self.call_after_refresh(self.action_add)

    def _show_backend(self) -> None:
        backend = str(self.query_one("#source-backend", Select).value)
        if backend == self._shown_backend:
            return
        self._shown_backend = backend
        postgres = backend == "postgres"
        self.query_one("#d1-fields").display = not postgres
        self.query_one("#postgres-fields").display = postgres
        self.query_one("#postgres-host" if postgres else "#account-id", Input).focus()

    def _fields(self) -> tuple[str, ...]:
        # Keep the D1 field positions stable. Passwords are deliberately not stripped.
        d1 = tuple(self.query_one(f"#{name}", Input).value.strip() for name in (
            "account-id", "database", "api-token", "snapshot-path", "display-name",
        ))
        postgres = tuple(self.query_one(f"#postgres-{name}", Input).value.strip() for name in (
            "host", "port", "database", "user",
        ))
        return (*d1, str(self.query_one("#source-backend", Select).value), *postgres,
                self.query_one("#postgres-password", Input).value,
                self.query_one("#postgres-schema", Input).value.strip(),
                str(self.query_one("#postgres-sslmode", Select).value))

    def _message(self, text: str, error: bool = False) -> None:
        for secret in (self.query_one("#api-token", Input).value.strip(),
                       self.query_one("#postgres-password", Input).value):
            if secret:
                text = text.replace(secret, "[redacted]")
        widget = self.query_one("#add-source-message", Static)
        widget.update(text)
        widget.set_class(error, "error")

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for name in ("test", "add"):
            self.query_one(f"#{name}", Button).disabled = busy
        self.query_one("#add-source").display = not busy
        progress = self.query_one(ConnectionStatus)
        progress.display = busy
        if busy:
            fields = self._fields()
            target = fields[8] if fields[5] == "postgres" else fields[3] or fields[1]
            for secret in (fields[2], fields[10]):
                if secret:
                    target = target.replace(secret, "[redacted]")
            progress.start(target or "data source")
            progress.query_one(Button).focus()
        else:
            self.query_one("#add", Button).focus()


    def _invalidate(self) -> None:
        self._generation += 1
        if self._worker is not None:
            self._worker.cancel()
            self._worker = None
        if self._built is not None:
            release_source(self._built[1])
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

    @on(Select.Changed, "#source-backend")
    def _backend_selected(self) -> None:
        if self._connection_form_closed:
            return
        self._show_backend()
        self._fields_changed()

    @on(Select.Changed, "#postgres-sslmode")
    def _sslmode_selected(self) -> None:
        self._fields_changed()

    @on(Select.Changed, "#postgres-profile")
    def _postgres_profile_selected(self, event: Select.Changed) -> None:
        if self._connection_form_closed:
            return
        self._invalidate()
        profile = self._postgres_profiles[event.value] if isinstance(event.value, int) else {}
        with self.prevent(Select.Changed, Input.Changed):
            self._fill_postgres_profile(profile)

    def _fill_postgres_profile(self, profile: dict[str, str]) -> None:
        defaults = {"host": "localhost", "port": "5432", "schema": "public"}
        for key in ("host", "port", "database", "user", "password", "schema"):
            self.query_one(f"#postgres-{key}", Input).value = profile.get(key, defaults.get(key, ""))
        self.query_one("#postgres-sslmode", Select).value = profile.get("sslmode", "prefer")
        self.query_one("#display-name", Input).value = profile.get("display", "")
        self._observed_fields = self._fields()
        self._message("Saved PostgreSQL credentials loaded. Test or Add to connect." if profile else "")

    @staticmethod
    def _postgres_profile(fields: tuple[str, ...]) -> dict[str, str]:
        return dict(zip(("host", "port", "database", "user", "password", "schema", "sslmode"), fields[6:]))

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
        account_id, database, api_token, path, display = fields[:5]
        if fields[5] == "postgres":
            profile = self._postgres_profile(fields)
            if not all(profile[key] for key in ("host", "port", "database", "user", "schema")):
                raise ValueError("Host, port, database, user and schema are required.")
            port = int(profile.pop("port"))
            if not 1 <= port <= 65535:
                raise ValueError("Port must be between 1 and 65535.")
            return self.session.build_postgres_source(**profile, port=port, display=display or None)
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
        self._message("Connecting… Cancel to return without saving.")
        self._worker = self.run_worker(
            self._connect(fields, self._generation, add),
            name="Source connection", group="source-connect", exit_on_error=False,
        )

    async def _connect(self, fields: tuple[str, ...], generation: int, add: bool) -> None:
        try:
            if self._built is not None and self._built[0] == fields:
                source = self._built[1]
            else:
                source = await build_connection(lambda: self._build(fields), timeout=self.CONNECTION_TIMEOUT)
                if self._connection_form_closed or generation != self._generation or fields != self._fields():
                    release_source(source)
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
                elif source.kind == "postgres-live" and fields != self._reconnect_fields:
                    profile = self._postgres_profile(fields)
                    profile["display"] = source.display
                    self.session.remember_postgres(profile)
                self._built = None  # ownership passes to the app's dismissal callback
                self._connection_form_closed = True
                self.dismiss(source)
            else:
                if source.schema_indexed:
                    tables = ", ".join(source.datasets) or "no user tables"
                    self._message(f"{source.display} · {len(source.datasets)} tables · {tables}")
                else:
                    self._message(f"{source.display} · Connection successful. Add to query and index schema in the background.")
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            if not self._connection_form_closed and generation == self._generation and fields == self._fields():
                self._message(f"Connection timed out after {self.CONNECTION_TIMEOUT:g}s. Retry to connect.", error=True)
        except (D1Error, PayloadError, OSError, RuntimeError, ValueError) as exc:
            if not self._connection_form_closed and generation == self._generation and fields == self._fields():
                self._message(self._connection_error(fields) if fields[5] == "postgres" else str(exc), error=True)
        except Exception:
            if not self._connection_form_closed and generation == self._generation and fields == self._fields():
                self._message(self._connection_error(fields), error=True)
        finally:
            if generation == self._generation:
                self._worker = None
                if not self._connection_form_closed:
                    self._set_busy(False)

    @staticmethod
    def _connection_error(fields: tuple[str, ...]) -> str:
        if fields[5] == "postgres":
            return ("Could not connect to PostgreSQL or save the connection. Check the host, port (1–65535), "
                    "database, user, password, schema, TLS mode and access permissions, then try again.")
        return "Could not connect. Check the account, database and token permissions, then try again."

    def action_cancel(self) -> None:
        self._connection_form_closed = True
        self._invalidate()
        self.query_one("#api-token", Input).value = ""
        self.query_one("#postgres-password", Input).value = ""
        self.dismiss(None)

    def on_unmount(self) -> None:
        self._connection_form_closed = True
        self._invalidate()

    @on(Button.Pressed, ".connection-cancel")
    def _cancel_connection(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_cancel()

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
