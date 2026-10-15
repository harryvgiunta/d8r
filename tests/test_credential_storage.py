"""Vault-backed snapshots: migration, rollback, missing secrets and session scope."""

from copy import deepcopy
from dataclasses import asdict
import json

import pytest

from d8r import storage
from d8r.ai.client import AIConfig
from d8r.credentials import CredentialError, CredentialVault
from d8r.engine.connectors import profile_id, validate_profile
from d8r.tui.session import Session


DATABASE = "12345678-1234-5678-9abc-123456789abc"
D1 = {"account_id": "account", "database": DATABASE, "display": "D1", "api_token": "d1-private-value"}
POSTGRES = {
    "host": "localhost", "port": "5432", "database": "app", "user": "reader",
    "password": "  postgres-private-value  ", "schema": "public", "sslmode": "prefer", "display": "PG",
}
BACKEND = {
    "backend": "oracle", "display": "Oracle", "options": {
        "host": "db.example", "port": 1521, "user": "reader", "password": "oracle-private-value",
        "dsn": "my-advanced-target", "arbitrary_driver_setting": {"innocent": ["nested-private-value", 7, False]},
    },
}
AI = AIConfig("https://provider.example/v1", "model", "ai-private-value")
FUNCTIONS = {"demo": [{"name": "retained", "params": [], "body": "\\from events", "description": "kept"}]}


def refuse_vault(*args, **kwargs):
    raise CredentialError("The OS credential vault is unavailable.")


def legacy_memory():
    return {"version": 1, "functions": deepcopy(FUNCTIONS), "d1_profiles": [dict(D1)],
            "postgres_profiles": [dict(POSTGRES)], "backend_profiles": [deepcopy(BACKEND)]}


def remember_all(session):
    session.remember_d1(**D1)
    session.remember_postgres(POSTGRES)
    session.remember_backend(BACKEND)
    session.update_settings(ai=AI)


def test_roundtrip_stores_only_metadata_and_opaque_references(sources, tmp_path):
    session = Session(sources, data_dir=tmp_path)
    remember_all(session)
    session.save_fn("retained", "", "\\from events", "kept")
    disk = session.storage_path.read_text() + session.settings_path.read_text()
    for private in (D1["api_token"], POSTGRES["password"], "oracle-private-value", "my-advanced-target",
                    "nested-private-value", "arbitrary_driver_setting", AI.api_key):
        assert private not in disk
    memory = json.loads(session.storage_path.read_text())
    assert memory["version"] == 2
    assert memory["backend_profiles"][0]["options"]["host"] == "db.example"
    assert memory["backend_profiles"][0]["connection_id"] == profile_id(BACKEND)
    assert all(profile["credential_ref"] for group in ("d1_profiles", "postgres_profiles", "backend_profiles")
               for profile in memory[group])
    restored = Session(sources, data_dir=tmp_path)
    assert restored.d1_profiles == [D1]
    assert restored.postgres_profiles == [POSTGRES]
    assert restored.backend_profiles == [validate_profile(BACKEND)]
    assert restored.ai_config == AI
    assert "retained" in restored.fns


def test_legacy_migration_secures_every_secret_without_plaintext_backup(sources, tmp_path):
    path = tmp_path / "memory.json"
    path.write_text(json.dumps(legacy_memory()))
    session = Session(sources, data_dir=tmp_path)
    assert not session.memory_error
    assert "backups" in session.credential_warning
    assert "retained" in session.fns
    assert session.postgres_profiles == [POSTGRES]
    assert session.backend_profiles == [validate_profile(BACKEND)]
    assert json.loads(path.read_text())["version"] == 2
    assert D1["api_token"] not in path.read_text()
    assert {item.name for item in tmp_path.iterdir()} == {"memory.json"}
    assert Session(sources, data_dir=tmp_path).backend_profiles == session.backend_profiles


@pytest.mark.parametrize("failure", ["vault", "file"])
def test_failed_legacy_migration_keeps_original_and_rolls_back_new_entries(
    sources, tmp_path, monkeypatch, credential_backend, failure,
):
    path = tmp_path / "memory.json"
    original = json.dumps(legacy_memory()).encode()
    path.write_bytes(original)
    if failure == "vault":
        original_put = CredentialVault.put
        writes = 0

        def fail_second(self, value):
            nonlocal writes
            writes += 1
            if writes == 2:
                refuse_vault()
            return original_put(self, value)

        monkeypatch.setattr(CredentialVault, "put", fail_second)
    else:
        def fail_replace(*args):
            raise PermissionError("private filesystem error")
        monkeypatch.setattr(storage.os, "replace", fail_replace)
    session = Session(sources, data_dir=tmp_path)
    assert not session.memory_error
    assert "plaintext" in session.credential_warning
    assert "retained" in session.fns
    assert session.d1_profiles == [D1]
    assert path.read_bytes() == original
    assert not credential_backend.values
    monkeypatch.setattr(CredentialVault, "put", refuse_vault)
    with pytest.raises(CredentialError):
        session.save_fn("new", "", "\\from events", "")
    assert path.read_bytes() == original
    assert {item.name for item in tmp_path.iterdir()} == {"memory.json"}


def test_invalid_legacy_file_is_validated_before_any_vault_write(tmp_path, monkeypatch):
    contents = legacy_memory()
    contents["backend_profiles"].append({"backend": "not-supported", "options": {}, "display": "bad"})
    path = tmp_path / "memory.json"
    original = json.dumps(contents).encode()
    path.write_bytes(original)

    def unexpected_put(*args):
        raise AssertionError("invalid file must not touch the vault")

    monkeypatch.setattr(CredentialVault, "put", unexpected_put)
    store = storage.MemoryStore(tmp_path)
    assert store.error
    assert path.read_bytes() == original


@pytest.mark.parametrize("failure", ["locked", "missing"])
def test_missing_credentials_keep_targets_functions_and_references(
    sources, tmp_path, monkeypatch, credential_backend, failure,
):
    original = Session(sources, data_dir=tmp_path)
    remember_all(original)
    target = profile_id(BACKEND)
    original.restore_source(target)
    original.save_fn("retained", "", "\\from events", "kept")
    original.save_workspace(source=target, dialect="oracle", document="unchanged draft")
    before = json.loads(original.storage_path.read_text())
    if failure == "locked":
        monkeypatch.setattr(CredentialVault, "get", refuse_vault)
    else:
        credential_backend.values.clear()
    session = Session(sources, data_dir=tmp_path)
    assert not session.memory_error
    assert session.credential_warning
    assert session.source_key() == target
    assert not session.source_connected()
    assert "retained" in session.fns
    saved = session.saved_source_profile(target)
    assert saved["credentials_missing"] is True
    assert saved["options"]["host"] == "db.example"
    assert profile_id(saved) == target
    assert target in {key for _, key in session.source_options()}
    assert session.postgres_profiles[0]["credentials_missing"] is True
    assert session.d1_profiles[0]["credentials_missing"] is True
    monkeypatch.setattr(CredentialVault, "put", refuse_vault)
    session.save_fn("second", "", "\\from events", "")
    session.update_settings(default_rows=17)
    after = json.loads(session.storage_path.read_text())
    for group in ("d1_profiles", "postgres_profiles", "backend_profiles"):
        assert after[group] == before[group]
    assert session.workspace["document"] == "unchanged draft"


def test_loaded_credentials_allow_unrelated_saves_without_any_vault_access(sources, tmp_path, monkeypatch):
    session = Session(sources, data_dir=tmp_path)
    remember_all(session)
    before_memory = json.loads(session.storage_path.read_text())
    before_ai = json.loads(session.settings_path.read_text())["ai"]
    for method in ("get", "put", "delete"):
        monkeypatch.setattr(CredentialVault, method, refuse_vault)
    session.save_fn("retained", "", "\\from events", "")
    session.update_settings(intellisense=False)
    assert session.ai_config == AI
    after = json.loads(session.storage_path.read_text())
    for group in ("d1_profiles", "postgres_profiles", "backend_profiles"):
        assert after[group] == before_memory[group]
    assert json.loads(session.settings_path.read_text())["ai"] == before_ai


@pytest.mark.parametrize("failure", ["stale", "file"])
@pytest.mark.parametrize("kind", ["postgres", "ai"])
def test_failed_rotation_keeps_old_secret_and_removes_new_entry(
    sources, tmp_path, monkeypatch, credential_backend, failure, kind,
):
    writer = Session(sources, data_dir=tmp_path)
    writer.remember_postgres(POSTGRES)
    writer.update_settings(ai=AI)
    candidate = Session(sources, data_dir=tmp_path)
    if failure == "stale":
        if kind == "postgres":
            writer.save_fn("retained", "", "\\from events", "")
        else:
            writer.update_settings(default_rows=11)
    else:
        def deny_replace(*args):
            raise PermissionError("private error")
        monkeypatch.setattr(storage.os, "replace", deny_replace)
    values = dict(credential_backend.values)
    memory_before, settings_before = writer.storage_path.read_bytes(), writer.settings_path.read_bytes()
    with pytest.raises(ValueError):
        if kind == "postgres":
            candidate.remember_postgres({**POSTGRES, "password": "rotated-value"})
        else:
            candidate.update_settings(ai=AIConfig(AI.base_url, AI.model, "rotated-value"))
    assert credential_backend.values == values
    assert writer.storage_path.read_bytes() == memory_before
    assert writer.settings_path.read_bytes() == settings_before
    assert candidate.postgres_profiles == [POSTGRES]
    assert candidate.ai_config == AI


def test_cleanup_failure_does_not_relabel_committed_rotation(sources, tmp_path, monkeypatch):
    session = Session(sources, data_dir=tmp_path)
    session.remember_postgres(POSTGRES)
    monkeypatch.setattr(CredentialVault, "delete", refuse_vault)
    rotated = {**POSTGRES, "password": "new-password"}
    session.remember_postgres(rotated)
    assert session.postgres_profiles == [rotated]
    assert "unused credential" in session.credential_warning
    assert Session(sources, data_dir=tmp_path).postgres_profiles == [rotated]


def test_missing_ai_key_blocks_requests_but_remains_editable(sources, tmp_path, credential_backend):
    session = Session(sources, data_dir=tmp_path)
    session.update_settings(ai=AI)
    reference = json.loads(session.settings_path.read_text())["ai"]["api_key_ref"]
    credential_backend.values.clear()
    session = Session(sources, data_dir=tmp_path)
    assert session.ai_credentials_missing
    assert session.ai_config.base_url == AI.base_url
    assert session.ai_config.model == AI.model
    with pytest.raises(CredentialError, match="saved AI API key"):
        session.ai_config.validate()
    session.update_settings(default_rows=19)
    assert json.loads(session.settings_path.read_text())["ai"]["api_key_ref"] == reference
    session.update_settings(ai=AIConfig(AI.base_url, AI.model, ""))
    assert not session.ai_credentials_missing
    restored = Session(sources, data_dir=tmp_path)
    assert not restored.ai_credentials_missing
    restored.ai_config.validate()
    assert restored.ai_config.api_key == ""


def test_keyless_targets_remain_usable_without_a_credential_vault(sources, tmp_path, monkeypatch):
    for operation in ("put", "get", "delete"):
        monkeypatch.setattr(CredentialVault, operation, refuse_vault)
    session = Session(sources, data_dir=tmp_path)
    session.remember_postgres({**POSTGRES, "password": ""})
    local_provider = AIConfig("http://127.0.0.1:11434/v1", "local-model")
    session.update_settings(ai=local_provider)
    restored = Session(sources, data_dir=tmp_path)
    assert restored.postgres_profiles == [{**POSTGRES, "password": ""}]
    assert not restored.saved_source_profile(restored._postgres_profile_id(POSTGRES)).get("credentials_missing")
    assert restored.ai_config == local_provider
    assert not restored.ai_credentials_missing
    assert not restored.credential_warning


@pytest.mark.parametrize("existing", [False, True])
def test_session_only_secrets_never_leak_through_unrelated_saves(
    sources, tmp_path, monkeypatch, existing,
):
    session = Session(sources, data_dir=tmp_path)
    if existing:
        remember_all(session)
    transient_ai = AIConfig("https://session.example/v1", "temporary", "temporary-ai")
    with monkeypatch.context() as patch:
        patch.setattr(CredentialVault, "put", refuse_vault)
        session.remember_d1(**{**D1, "api_token": "temporary-d1"}, persist=False)
        session.remember_postgres({**POSTGRES, "password": "temporary-pg"}, persist=False)
        session.remember_backend({**BACKEND, "options": {**BACKEND["options"], "password": "temporary-backend"}}, persist=False)
        session.update_settings(ai=transient_ai, persist_ai=False)
        session.save_fn("retained", "", "\\from events", "kept")
        session.update_settings(default_rows=15)
    assert session.ai_config == transient_ai
    assert session.d1_profiles[0]["api_token"] == "temporary-d1"
    assert session.postgres_profiles[0]["password"] == "temporary-pg"
    assert session.backend_profiles[0]["options"]["password"] == "temporary-backend"
    disk = session.storage_path.read_text() + session.settings_path.read_text()
    assert "temporary-" not in disk
    assert "session.example" not in disk
    restored = Session(sources, data_dir=tmp_path)
    assert "retained" in restored.fns
    assert restored.default_rows == 15
    assert restored.d1_profiles == ([D1] if existing else [])
    assert restored.postgres_profiles == ([POSTGRES] if existing else [])
    assert restored.backend_profiles == ([validate_profile(BACKEND)] if existing else [])
    assert restored.ai_config == (AI if existing else AIConfig())


def test_session_only_profile_can_later_be_saved_explicitly(sources, tmp_path):
    session = Session(sources, data_dir=tmp_path)
    session.remember_postgres(POSTGRES, persist=False)
    assert not session.storage_path.exists()
    session.remember_postgres(POSTGRES)
    assert Session(sources, data_dir=tmp_path).postgres_profiles == [POSTGRES]


def test_data_directories_cannot_resolve_each_others_references(sources, tmp_path):
    left, right = tmp_path / "left", tmp_path / "right"
    session = Session(sources, data_dir=left)
    session.remember_postgres(POSTGRES)
    right.mkdir()
    (right / "memory.json").write_bytes(session.storage_path.read_bytes())
    other = Session(sources, data_dir=right)
    assert not other.memory_error
    assert other.postgres_profiles[0]["credentials_missing"] is True
    assert Session(sources, data_dir=left).postgres_profiles == [POSTGRES]


def test_provider_change_does_not_reuse_old_key_when_not_supplied(tmp_path):
    store = storage.SettingsStore(tmp_path)
    store.save({"ai": asdict(AI)})
    old_reference = json.loads(store.path.read_text())["ai"]["api_key_ref"]
    store.save({"ai": {"base_url": "https://different.example/v1", "model": "other"}})
    restored = storage.SettingsStore(tmp_path)
    assert restored.document["ai"]["api_key"] == ""
    assert json.loads(store.path.read_text())["ai"]["api_key_ref"] != old_reference


def test_failed_ai_migration_preserves_preferences_and_plaintext_original(sources, tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    contents = json.dumps({"intellisense": False, "default_rows": 23, "ai": asdict(AI)}).encode()
    path.write_bytes(contents)
    monkeypatch.setattr(CredentialVault, "put", refuse_vault)
    session = Session(sources, data_dir=tmp_path)
    assert not session.memory_error
    assert not session.intellisense
    assert session.default_rows == 23
    assert session.ai_config == AI
    assert "plaintext" in session.credential_warning
    session.update_settings(ai=AIConfig(AI.base_url, AI.model, "session-key"), persist_ai=False)
    assert session.ai_config.api_key == "session-key"
    with pytest.raises(CredentialError):
        session.update_settings(default_rows=24)
    assert path.read_bytes() == contents


def test_oversized_vault_failure_has_explicit_session_only_path(sources, tmp_path, monkeypatch, credential_backend):
    profile = {**BACKEND, "options": {**BACKEND["options"], "arbitrary_driver_setting": {"opaque": "x" * 4000}}}
    session = Session(sources, data_dir=tmp_path)

    def deny_large_value(service, username, password):
        assert len(password) > 2560
        raise RuntimeError("native store size limit")

    monkeypatch.setattr(credential_backend, "set_password", deny_large_value)
    with pytest.raises(CredentialError):
        session.remember_backend(profile)
    assert not session.storage_path.exists()
    session.remember_backend(profile, persist=False)
    assert session.backend_profiles == [validate_profile(profile)]
    session.save_fn("retained", "", "\\from events", "")
    assert not Session(sources, data_dir=tmp_path).backend_profiles


def test_old_reference_is_deleted_only_after_successful_file_replacement(sources, tmp_path, monkeypatch):
    session = Session(sources, data_dir=tmp_path)
    session.remember_postgres(POSTGRES)
    reference = json.loads(session.storage_path.read_text())["postgres_profiles"][0]["credential_ref"]
    vault = CredentialVault(tmp_path)
    replace = storage.os.replace

    def replace_with_old_secret_intact(source, destination):
        assert vault.get(reference) == POSTGRES["password"]
        replace(source, destination)

    monkeypatch.setattr(storage.os, "replace", replace_with_old_secret_intact)
    session.remember_postgres({**POSTGRES, "password": "rotated-value"})
    assert vault.get(reference) is None
    assert Session(sources, data_dir=tmp_path).postgres_profiles[0]["password"] == "rotated-value"
