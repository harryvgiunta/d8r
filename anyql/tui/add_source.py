"""The `ctrl+o` modal: bring a real Cloudflare D1 database into the registry.

Two shapes, one form: a live database reached over Cloudflare's HTTPS API, or a
local SQLite snapshot of one (`wrangler d1 execute --local`). A snapshot skips
the network entirely, so the same modal serves an offline user.

`Add` dismisses with the built source, which the app then registers; `Cancel`
dismisses with `None` and changes nothing. The API token lives only in the
masked input and inside the built source's client — it is never persisted, never
logged, and never echoed back into a message this screen shows.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from anyql.engine import D1Error, DataSource, PayloadError


class AddSourceModal(ModalScreen):
    """The credential form; `Test connection` validates before `Add` registers."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, session, **kwargs) -> None:
        super().__init__(**kwargs)
        self.session = session
        # The last successfully built source and the exact field values that
        # produced it, so `Test connection` followed by `Add` connects once.
        self._built: tuple[tuple[str, ...], DataSource] | None = None

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="add-source"):
            yield Label("Add a data source", id="add-source-title")
            yield Static(
                "A live Cloudflare D1 database — or a local SQLite snapshot of one.",
                id="add-source-note",
                markup=False,
            )
            yield Label("Account ID", classes="field-label")
            yield Input(placeholder="32-character Cloudflare account id", id="account-id")
            yield Label("Database", classes="field-label")
            yield Input(placeholder="database name or uuid", id="database")
            yield Label("API token", classes="field-label")
            yield Input(placeholder="sent to Cloudflare only, never stored", password=True, id="api-token")
            yield Label("Local snapshot", classes="field-label")
            yield Input(placeholder="optional .sqlite path — skips the network", id="snapshot-path")
            yield Label("Display name", classes="field-label")
            yield Input(placeholder="optional label for the datasource list", id="display-name")
            yield Static("", id="add-source-message", markup=False)
            with Horizontal(id="add-source-buttons"):
                yield Button("Test connection", id="test")
                yield Button("Add", variant="primary", id="add")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#account-id", Input).focus()

    # -- fields -------------------------------------------------------------

    def _fields(self) -> tuple[str, ...]:
        return (
            self.query_one("#account-id", Input).value.strip(),
            self.query_one("#database", Input).value.strip(),
            self.query_one("#api-token", Input).value,
            self.query_one("#snapshot-path", Input).value.strip(),
            self.query_one("#display-name", Input).value.strip(),
        )

    def _message(self, text: str, error: bool = False) -> None:
        widget = self.query_one("#add-source-message", Static)
        widget.update(text)
        widget.set_class(error, "error")

    def _ensure(self) -> DataSource | None:
        """The source for the current field values, building it at most once."""
        fields = self._fields()
        if self._built is not None and self._built[0] == fields:
            return self._built[1]
        account_id, database, api_token, path, display = fields
        try:
            if path:
                source = self.session.build_sqlite_source(path, display)
            elif account_id and database and api_token:
                source = self.session.build_live_source(account_id, api_token, database, display)
            else:
                raise ValueError("Account ID, Database and API token are required for a live source")
        except (D1Error, PayloadError, OSError, RuntimeError, ValueError) as exc:
            self._built = None
            self._message(str(exc), error=True)
            return None
        except Exception as exc:  # last-resort guard: a UI may not crash or exit
            self._built = None
            self._message(f"{type(exc).__name__}: {exc}", error=True)
            return None
        self._built = (fields, source)
        return source

    # -- actions ------------------------------------------------------------

    def action_cancel(self) -> None:
        """Close without registering anything."""
        self.dismiss(None)

    def action_test(self) -> None:
        """Connect once and report what the database holds."""
        source = self._ensure()
        if source is None:
            return
        tables = ", ".join(source.datasets) or "no user tables"
        self._message(f"{source.display} · {len(source.datasets)} tables · {tables}")

    def action_add(self) -> None:
        """Hand the built source back to the app for registration."""
        source = self._ensure()
        if source is None:
            return
        self.dismiss(source)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "test":
            self.action_test()
        elif event.button.id == "add":
            self.action_add()
        else:
            self.action_cancel()
