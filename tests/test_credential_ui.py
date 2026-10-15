"""Credential choices are explicit in real forms; no network or native vault is used."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Button, Checkbox, Input, Select, Static, TextArea

from d8r.ai.client import AIConfig
from d8r.credentials import CredentialError, CredentialVault
from d8r.engine import DataSource
from d8r.tui.add_source import AddSourceModal
from d8r.tui.app import D8RApp
from d8r.tui.connection import build_saved_source
from d8r.tui.session import Session
from d8r.tui.settings import AIProviderScreen


SECRET = "private-credential-value"
DATABASE = "11111111-2222-3333-4444-555555555555"
POSTGRES = {
    "host": "localhost", "port": "5432", "database": "orders", "user": "reader",
    "password": SECRET, "schema": "public", "sslmode": "prefer", "display": "Orders",
}
GENERIC = {
    "backend": "mysql", "display": "Orders", "options": {
        "host": "localhost", "port": 3306, "database": "orders", "user": "reader",
        "password": SECRET, "connect_timeout": 12,
    },
}


class FormApp(App):
    def compose(self) -> ComposeResult:
        yield Input(id="editor")


def install_source(monkeypatch, session, backend):
    built, closed = [], []

    def build(*args, **kwargs):
        source = DataSource(
            id="orders", display="Orders", doc="", dir=Path("."),
            kind={"d1": "d1-live", "postgres": "postgres-live", "mysql": "ibis"}[backend],
            dialect="sqlite" if backend == "d1" else backend,
            con=SimpleNamespace(disconnect=lambda: closed.append(True)),
            d1=SimpleNamespace(account_id="account", database_uuid=DATABASE,
                               close=lambda: closed.append(True)) if backend == "d1" else None,
            postgres={key: value for key, value in POSTGRES.items() if key not in {"password", "display"}},
        )
        built.append(source)
        return source

    method = {"d1": "build_live_source", "postgres": "build_postgres_source", "mysql": "build_backend_source"}[backend]
    monkeypatch.setattr(session, method, build)
    return built, closed


async def fill_source(modal, pilot, backend):
    modal.query_one("#source-backend", Select).value = backend
    await pilot.pause()
    if backend == "d1":
        values = {"account-id": "account", "database": DATABASE, "api-token": SECRET}
    elif backend == "postgres":
        values = {"postgres-database": "orders", "postgres-user": "reader", "postgres-password": SECRET}
    else:
        values = {"backend-mysql-database": "orders", "backend-mysql-user": "reader",
                  "backend-mysql-password": SECRET, "backend-options": '{"connect_timeout":12}'}
    for name, value in values.items():
        modal.query_one(f"#{name}", Input).value = value
    await pilot.pause()


@pytest.mark.parametrize("backend", ["d1", "postgres", "mysql"])
def test_test_reuses_connection_and_add_alone_saves_securely(sources, tmp_path, monkeypatch, credential_backend, backend):
    session = Session(sources, data_dir=tmp_path)
    built, closed = install_source(monkeypatch, session, backend)
    transferred = []

    async def scenario():
        app = FormApp()
        async with app.run_test(size=(140, 45)) as pilot:
            modal = AddSourceModal(session)
            await app.push_screen(modal, transferred.append)
            await fill_source(modal, pilot, backend)
            assert modal.query_one("#remember-credentials", Checkbox).value
            assert all(field.password for field in (
                modal.query_one("#api-token", Input), modal.query_one("#postgres-password", Input),
                modal.query_one("#backend-mysql-password", Input), modal.query_one("#backend-options", Input),
            ))
            modal.action_test()
            await app.workers.wait_for_complete()
            assert len(built) == 1
            assert not credential_backend.values
            assert not session.storage_path.exists()
            modal.action_add()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert transferred == built
            assert len(built) == 1
            assert not closed
            assert credential_backend.values
            assert SECRET not in session.storage_path.read_text()
            if backend == "mysql":
                assert "connect_timeout" not in session.storage_path.read_text()

    asyncio.run(scenario())


def test_test_cancel_and_remember_toggle_dispose_without_vault_save(sources, tmp_path, monkeypatch, credential_backend):
    session = Session(sources, data_dir=tmp_path)
    built, closed = install_source(monkeypatch, session, "d1")

    async def scenario():
        app = FormApp()
        async with app.run_test(size=(140, 45)) as pilot:
            modal = AddSourceModal(session)
            await app.push_screen(modal)
            await fill_source(modal, pilot, "d1")
            modal.action_test()
            await app.workers.wait_for_complete()
            generation = modal._generation
            modal.query_one("#remember-credentials", Checkbox).value = False
            await pilot.pause()
            assert modal._generation > generation
            assert closed == [True]
            assert modal._built is None
            modal.action_test()
            await app.workers.wait_for_complete()
            assert len(built) == 2
            modal.action_cancel()
            await pilot.pause()
            assert closed == [True, True]
            assert not credential_backend.values
            assert not session.storage_path.exists()

    asyncio.run(scenario())


@pytest.mark.parametrize("backend", ["d1", "postgres", "mysql"])
def test_vault_failure_keeps_form_usable_and_explicit_session_only_never_persists(
    sources, tmp_path, monkeypatch, credential_backend, backend,
):
    session = Session(sources, data_dir=tmp_path)
    built, closed = install_source(monkeypatch, session, backend)
    transferred = []

    def unavailable(self, value):
        raise CredentialError("System keyring is unavailable.")

    monkeypatch.setattr(CredentialVault, "put", unavailable)

    async def scenario():
        app = FormApp()
        async with app.run_test(size=(140, 45)) as pilot:
            modal = AddSourceModal(session)
            await app.push_screen(modal, transferred.append)
            await fill_source(modal, pilot, backend)
            modal.action_add()
            await app.workers.wait_for_complete()
            assert app.screen is modal
            assert not modal.query_one("#add", Button).disabled
            message = str(modal.query_one("#add-source-message", Static).content)
            assert "keyring" in message and "uncheck Remember" in message
            assert SECRET not in message
            assert not transferred
            modal.query_one("#remember-credentials", Checkbox).value = False
            await pilot.pause()
            modal.action_add()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert transferred == [built[-1]]
            assert len(built) == 2 and closed == [True]
            session.save_fn("events_copy", "", "\\from events", "")
            session.update_settings(intellisense=False)
            restored = Session(sources, data_dir=tmp_path)
            assert not restored.d1_profiles
            assert not restored.postgres_profiles
            assert not restored.backend_profiles
            assert not credential_backend.values
            assert SECRET not in session.storage_path.read_text()

    asyncio.run(scenario())


def test_provider_secure_save_failure_then_session_only_survives_unrelated_settings(
    sources, tmp_path, monkeypatch, credential_backend,
):
    import httpx
    from tests.test_ai_tui import completion, finished, install_provider

    requests = []

    def provider(request):
        requests.append(request)
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=completion("Ready."))

    install_provider(monkeypatch, provider)
    session = Session(sources, data_dir=tmp_path)

    def unavailable(self, value):
        raise CredentialError("System keyring is locked.")

    monkeypatch.setattr(CredentialVault, "put", unavailable)

    async def scenario():
        app = D8RApp(session)
        async with app.run_test(size=(150, 58)) as pilot:
            form = AIProviderScreen(app)
            await app.push_screen(form)
            form.query_one("#ai-base-url", Input).value = "https://provider.invalid/v1"
            form.query_one("#ai-model", Input).value = "session-model"
            form.query_one("#ai-api-key", Input).value = SECRET
            assert form.query_one("#ai-api-key", Input).password
            assert form.query_one("#ai-remember-credentials", Checkbox).value
            form._save_ai()
            await pilot.pause()
            assert app.screen is form
            message = str(form.query_one("#ai-settings-error", Static).content)
            assert "keyring" in message and "uncheck Remember" in message
            assert SECRET not in message
            assert session.ai_config.api_key == ""
            form.query_one("#ai-remember-credentials", Checkbox).value = False
            form._save_ai()
            await pilot.pause()
            assert app.screen is not form
            assert session.ai_config.api_key == SECRET
            assert session.ai_config.model == "session-model"
            assert app.update_settings(intellisense=False, ai_allow_sample_data=False)
            assert session.ai_config.api_key == SECRET
            app.action_ai()
            await pilot.pause()
            app.ai_panel.query_one("#ai-input", TextArea).load_text("Say ready.")
            app.ai_panel.action_send()
            await finished(app.ai_panel)
            assert len(requests) == 1
            assert requests[0].headers["Authorization"] == f"Bearer {SECRET}"
            restored = Session(sources, data_dir=tmp_path)
            assert restored.ai_config.api_key == ""
            assert restored.ai_config.model != "session-model"
            assert not credential_backend.values
            assert SECRET not in session.settings_path.read_text()

    asyncio.run(scenario())


def test_provider_import_cancel_is_form_only(sources, tmp_path, monkeypatch, credential_backend):
    session = Session(sources, data_dir=tmp_path)
    original = session.ai_config
    monkeypatch.setattr("d8r.tui.settings.import_yolo_config", lambda: AIConfig(
        base_url="https://provider.invalid/v1", model="imported", api_key=SECRET,
    ))

    async def scenario():
        app = D8RApp(session)
        async with app.run_test(size=(150, 58)) as pilot:
            form = AIProviderScreen(app)
            await app.push_screen(form)
            form._import_ai()
            assert form.query_one("#ai-api-key", Input).value == SECRET
            assert form.query_one("#ai-api-key", Input).password
            form.query_one("#ai-remember-credentials", Checkbox).value = False
            form.action_cancel()
            await pilot.pause()
            assert session.ai_config == original
            assert not credential_backend.values
            assert not session.settings_path.exists()

    asyncio.run(scenario())


@pytest.mark.parametrize("backend", ["d1", "postgres", "mysql"])
def test_missing_credentials_block_startup_run_and_selection_without_losing_document(
    sources, tmp_path, monkeypatch, credential_backend, backend,
):
    session = Session(sources, data_dir=tmp_path)
    if backend == "d1":
        session.remember_d1("account", DATABASE, "Orders", SECRET)
    elif backend == "postgres":
        session.remember_postgres(POSTGRES)
    else:
        session.remember_backend(GENERIC)
    target = next(key for _, key in session.source_options() if key not in sources)
    document = "\\from orders\n\\limit 7"
    session.save_workspace(source=target, dialect="mysql" if backend == "mysql" else "postgres" if backend == "postgres" else "sqlite",
                           document=document)
    credential_backend.values.clear()
    restored = Session(sources, data_dir=tmp_path)
    profile = restored.saved_source_profile(target)
    assert profile["credentials_missing"]
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("Missing credentials must never attempt a connection")

    for method in ("build_live_source", "build_postgres_source", "build_backend_source"):
        monkeypatch.setattr(restored, method, forbidden)
    with pytest.raises(CredentialError, match="credentials"):
        build_saved_source(restored, profile=profile)

    async def scenario():
        app = D8RApp(restored)
        async with app.run_test(size=(150, 58)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert not isinstance(app.screen, AddSourceModal)
            assert restored.source_key() == target
            assert app.editor.text == document
            identity = app._document_identity
            assert "credentials" in str(app.query_one("#status", Static).content)
            assert not attempts
            app.action_run()
            await pilot.pause()
            form = app.screen
            assert isinstance(form, AddSourceModal)
            assert not form._busy and not attempts
            assert form._credentials_missing
            if backend == "postgres":
                assert form.query_one("#postgres-database", Input).value == "orders"
                assert form.query_one("#postgres-password", Input).value == ""
            elif backend == "mysql":
                assert form.query_one("#backend-mysql-database", Input).value == "orders"
                assert form.query_one("#backend-mysql-password", Input).value == ""
                assert form.query_one("#backend-options", Input).value == "{}"
            form.action_test()
            await app.workers.wait_for_complete()
            assert not attempts
            form.action_cancel()
            await pilot.pause()
            assert restored.source_key() == target
            assert app.editor.text == document and app._document_identity == identity
            assert not restored.history
            app.select_source("demo")
            await pilot.pause()
            app.select_source(target)
            await pilot.pause()
            assert isinstance(app.screen, AddSourceModal)
            assert not attempts
            app.screen.action_cancel()
            await pilot.pause()
            assert restored.source_key() == target
            assert app.editor.text == document and app._document_identity == identity

    asyncio.run(scenario())


def test_missing_ai_key_blocks_provider_call_and_requires_reentry(sources, tmp_path, monkeypatch, credential_backend):
    session = Session(sources, data_dir=tmp_path)
    session.update_settings(ai=AIConfig(base_url="https://provider.invalid/v1", model="saved", api_key=SECRET))
    credential_backend.values.clear()
    restored = Session(sources, data_dir=tmp_path)
    assert restored.ai_credentials_missing
    attempts = []

    def forbidden(config):
        attempts.append(True)
        raise AssertionError("Missing AI keys must not reach the provider")

    monkeypatch.setattr("d8r.ai.client.create_client", forbidden)

    async def scenario():
        app = D8RApp(restored)
        async with app.run_test(size=(150, 58)) as pilot:
            app.action_ai()
            await pilot.pause()
            panel = app.ai_panel
            panel.query_one("#ai-input", TextArea).load_text("Do not send without credentials.")
            panel.action_send()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert not attempts
            assert not panel.busy
            assert "key" in str(panel.query_one("#ai-status", Static).content).lower()
            assert SECRET not in str(panel.query_one("#ai-status", Static).content)
            form = AIProviderScreen(app)
            await app.push_screen(form)
            assert form.query_one("#ai-model", Input).value == "saved"
            assert form.query_one("#ai-api-key", Input).value == ""
            assert form.query_one("#ai-api-key", Input).password
            form._save_ai()
            await pilot.pause()
            assert app.screen is form
            assert "Re-enter" in str(form.query_one("#ai-settings-error", Static).content)
            assert restored.ai_credentials_missing
            # Choosing a different, explicitly keyless local provider is not
            # silently retrying the saved authenticated endpoint without its key.
            form.query_one("#ai-base-url", Input).value = "http://127.0.0.1:11434/v1"
            form.query_one("#ai-remember-credentials", Checkbox).value = False
            form._save_ai()
            await pilot.pause()
            assert app.screen is not form
            assert restored.ai_config.base_url == "http://127.0.0.1:11434/v1"
            assert restored.ai_config.api_key == ""
            assert not restored.ai_credentials_missing
            assert not attempts

    asyncio.run(scenario())


@pytest.mark.parametrize("backend", ["d1", "postgres", "mysql"])
def test_unchanged_saved_reconnect_does_not_rewrite_vault(sources, tmp_path, monkeypatch, credential_backend, backend):
    session = Session(sources, data_dir=tmp_path)
    if backend == "d1":
        session.remember_d1("account", DATABASE, "Orders", SECRET)
    elif backend == "postgres":
        session.remember_postgres(POSTGRES)
    else:
        session.remember_backend(GENERIC)
    target = next(key for _, key in session.source_options() if key not in sources)
    profile = session.saved_source_profile(target)
    before = dict(credential_backend.values)
    saved = session.storage_path.read_bytes()
    built, closed = install_source(monkeypatch, session, backend)
    transferred = []

    def forbidden(self, value):
        raise AssertionError("An unchanged saved reconnect must not write credentials")

    monkeypatch.setattr(CredentialVault, "put", forbidden)

    async def scenario():
        app = FormApp()
        async with app.run_test(size=(140, 45)) as pilot:
            await app.push_screen(AddSourceModal(session, profile=profile), transferred.append)
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert transferred == built and len(built) == 1
            assert not closed
            assert credential_backend.values == before
            assert session.storage_path.read_bytes() == saved

    asyncio.run(scenario())
