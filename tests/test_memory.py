"""Durable functions, credentials, and settings, including failed-write safety."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from d8r.ai.client import AIConfig
from d8r import storage
from d8r.query import parse_query
from d8r.tui.palette import view_for
from d8r.tui.session import Session


DATABASE = "12345678-1234-5678-9abc-123456789abc"
BODY = "\\from events\n\\select @first as first, @second as second\n\\limit 2"


def test_functions_execute_after_restart_and_deletion_is_durable(sources):
    original = Session(sources)
    original.save_fn("pair", "first, second", BODY, "ordered arguments")
    original.save_fn("events_copy", "", "\\from events\n\\select user_id", "")
    original.remember_d1("account", DATABASE, "Production")

    restored = Session(sources)
    assert tuple(restored.fns["pair"].params) == ("first", "second")
    assert restored.fns["pair"].body == BODY
    assert restored.fns["pair"].doc == "ordered arguments"
    result = restored.run("\\from pair('hello', 42)\n\\select *")
    assert result.error == ""
    assert result.rows == [["hello", 42], ["hello", 42]]
    restored.delete_fn("pair")

    after_delete = Session(sources)
    assert list(after_delete.fns) == ["events_copy"]
    after_delete.update_settings(default_rows=0)
    assert after_delete.d1_profiles == original.d1_profiles
    missing = after_delete.run("\\from pair('hello', 42)\n\\select *")
    assert missing.error
    remaining = after_delete.run("\\from events_copy()\n\\select *")
    assert remaining.error == ""
    assert remaining.total == 100


def test_restoring_does_not_discard_functions_for_an_inactive_schema(sources):
    original = Session(sources)
    original.set_active("mysql")
    body = "\\from products\n\\select *\n\\limit 2"
    original.save_fn("products_copy", "", body, "foreign source")

    restored = Session(sources)
    assert restored.active_id == "demo"
    assert restored.memory_error == ""
    assert "products_copy" not in restored.fns
    restored.set_active("mysql")
    assert restored.fns["products_copy"].body == body
    result = restored.run("\\from products_copy()\n\\select *")
    assert result.error == ""
    assert result.total == 2


@pytest.mark.parametrize("operation", ["save", "delete", "profile"])
def test_failed_replace_preserves_file_registry_and_profiles(sources, monkeypatch, operation):
    session = Session(sources)
    saved = session.save_fn("pair", "first, second", BODY, "original")
    session.remember_d1("account", DATABASE, "Original", "original-token")
    before = session.storage_path.read_bytes()
    profiles = [dict(item) for item in session.d1_profiles]

    def deny_replace(source, destination):
        raise PermissionError("disk is read-only")

    monkeypatch.setattr(storage.os, "replace", deny_replace)
    with pytest.raises(ValueError, match="Cannot save local memory") as failure:
        if operation == "save":
            session.save_fn("pair", "first, second", BODY, "changed")
        elif operation == "delete":
            session.delete_fn("pair")
        else:
            session.remember_d1("account", DATABASE, "Changed", "rotated-token")
    assert str(session.storage_path) in str(failure.value)
    assert session.storage_path.read_bytes() == before
    assert session.fns["pair"] == saved
    assert session.d1_profiles == profiles
    result = session.run("\\from pair('unchanged', 7)\n\\select *")
    assert result.error == ""
    assert result.rows == [["unchanged", 7], ["unchanged", 7]]
    restored = Session(sources)
    assert restored.fns["pair"].doc == "original"
    assert restored.d1_profiles == profiles


@pytest.mark.parametrize("contents", [
    b'{"version":',
    b'{"version":3,"functions":[],"d1_profiles":[]}',
    b'{"version":true,"functions":[],"d1_profiles":[]}',
    b'{"version":1,"version":1,"functions":[],"d1_profiles":[]}',
    b'{"version":1,"functions":[{"name":"bad","params":{},"body":"","description":""}],"d1_profiles":[]}',
    b'{"version":1,"functions":[],"d1_profiles":[{"account_id":"a","database":"12345678-1234-5678-9abc-123456789abc","display":"d","token":"secret"}]}',
    b'\xff',
])
def test_invalid_memory_stays_untouched_and_blocks_saves(sources, tmp_path, contents):
    path = tmp_path / "memory.json"
    path.write_bytes(contents)
    session = Session(sources, data_dir=tmp_path)
    assert str(path) in session.memory_error
    assert "restart" in session.memory_error
    assert session.fns == {}
    assert session.d1_profiles == []
    with pytest.raises(ValueError, match="repair"):
        session.save_fn("pair", "first, second", BODY, "")
    with pytest.raises(ValueError, match="repair"):
        session.remember_d1("account", DATABASE, "Production")
    assert path.read_bytes() == contents


def test_unreadable_memory_is_not_replaced(sources, tmp_path, monkeypatch):
    path = tmp_path / "memory.json"
    contents = b'{"version":1,"functions":[],"d1_profiles":[]}'
    path.write_bytes(contents)
    read_bytes = Path.read_bytes

    def denied(target):
        if target == path:
            raise PermissionError("access denied")
        return read_bytes(target)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", denied)
        session = Session(sources, data_dir=tmp_path)
        with pytest.raises(ValueError, match="restart"):
            session.save_fn("pair", "first, second", BODY, "")
    assert path.read_bytes() == contents


def test_d1_credentials_rotate_and_restore_without_connecting(sources, monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "environment-token")
    session = Session(sources)
    session.remember_d1("account", DATABASE, "Old label", "old-token")
    assert Session(sources).d1_profiles[0]["api_token"] == "old-token"
    session.remember_d1("account", DATABASE, "Production", "rotated-token")
    restored = Session(sources)
    assert restored.d1_profiles == [{
        "account_id": "account", "database": DATABASE,
        "display": "Production", "api_token": "rotated-token",
    }]
    assert "old-token" not in session.storage_path.read_text(encoding="utf-8")
    assert all(source.kind != "d1-live" for source in restored.sources.values())


def test_profiles_require_resolved_uuid_and_preserve_previous_memory(sources):
    session = Session(sources)
    session.remember_d1("account", DATABASE, "Production")
    before = session.storage_path.read_bytes()
    with pytest.raises(ValueError, match="UUID"):
        session.remember_d1("account", "database-name", "Unresolved")
    assert session.storage_path.read_bytes() == before
    assert session.d1_profiles == [{
        "account_id": "account", "database": DATABASE,
        "display": "Production", "api_token": "",
    }]


def test_stale_session_cannot_overwrite_newer_functions_or_profiles(sources):
    stale = Session(sources)
    writer = Session(sources)
    writer.save_fn("pair", "first, second", BODY, "newer definition")
    writer.remember_d1("account", DATABASE, "Production")
    before = writer.storage_path.read_bytes()
    with pytest.raises(ValueError, match="changed outside this session"):
        stale.save_fn("obsolete", "", "\\from events\n\\select *", "")
    assert stale.fns == {}
    assert writer.storage_path.read_bytes() == before

    restored = Session(sources)
    writer.delete_fn("pair")
    after_delete = writer.storage_path.read_bytes()
    with pytest.raises(ValueError, match="restart"):
        restored.remember_d1("account", DATABASE, "Stale label")
    assert restored.d1_profiles[0]["display"] == "Production"
    assert writer.storage_path.read_bytes() == after_delete
    assert Session(sources).fns == {}


def test_explicit_directory_overrides_environment_without_startup_writes(sources, tmp_path):
    path = tmp_path / "explicit"
    session = Session(sources, data_dir=path)
    assert not path.exists()
    session.save_fn("pair", "first, second", BODY, "")
    session.update_settings(intellisense=False)
    assert Session(sources).fns == {}
    restored = Session(sources, data_dir=path)
    assert "pair" in restored.fns
    assert restored.intellisense is False
    assert Session(sources).intellisense is True


def test_default_directory_ignores_legacy_platform_locations(tmp_path, monkeypatch):
    monkeypatch.delenv("D8R_DATA_DIR")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "legacy-windows"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "legacy-xdg"))
    assert storage.data_directory() == tmp_path / ".d8r"
    assert not (tmp_path / ".d8r").exists()


def test_data_directory_environment_expands_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("D8R_DATA_DIR", "~/custom-d8r")
    assert storage.data_directory() == tmp_path / "custom-d8r"


@pytest.mark.parametrize("kind", ["memory", "settings"])
def test_stale_lock_is_taken_over_and_fresh_lock_stays_busy(tmp_path, kind):
    """A crashed save's lock must not wedge saves forever; a live one must."""
    import os
    import time

    store = storage.MemoryStore(tmp_path) if kind == "memory" else storage.SettingsStore(tmp_path)
    if kind == "memory":
        def save():
            store.save({"demo": [{"name": "pair", "params": [], "body": "\\from events", "description": ""}]}, [])
    else:
        def save():
            store.save({"intellisense": False})
    lock = store.path.with_suffix(".lock")

    lock.touch()
    with pytest.raises(ValueError, match="busy"):
        save()
    assert lock.exists()

    old = time.time() - 2 * storage._STALE_SECONDS
    os.utime(lock, (old, old))
    save()
    assert not lock.exists()
    restored = type(store)(tmp_path)
    if kind == "memory":
        assert [fn["name"] for fn in restored.document["functions"]["demo"]] == ["pair"]
    else:
        assert restored.document["intellisense"] is False


@pytest.mark.parametrize("kind", ["memory", "settings"])
def test_orphan_temp_files_are_swept_and_fresh_ones_survive(tmp_path, kind):
    """Load removes debris older than the grace period, never a save in flight."""
    import os
    import time

    old = tmp_path / f".{kind}-crashed.tmp"
    fresh = tmp_path / f".{kind}-running.tmp"
    old.write_bytes(b"{}")
    fresh.write_bytes(b"{}")
    stale = time.time() - 2 * storage._STALE_SECONDS
    os.utime(old, (stale, stale))
    (storage.MemoryStore if kind == "memory" else storage.SettingsStore)(tmp_path)
    assert not old.exists()
    assert fresh.exists()


def test_legacy_memory_profiles_load_without_rewriting(sources, tmp_path):
    path = tmp_path / "memory.json"
    contents = json.dumps({
        "version": 1, "functions": [], "d1_profiles": [
            {"account_id": "account", "database": DATABASE, "display": "Legacy"},
        ],
    }).encode("utf-8")
    path.write_bytes(contents)
    session = Session(sources, data_dir=tmp_path)
    assert session.memory_error == ""
    assert session.d1_profiles[0]["api_token"] == ""
    assert path.read_bytes() == contents
    session.remember_d1("account", DATABASE, "Configured", "new-token")
    assert Session(sources, data_dir=tmp_path).d1_profiles[0]["api_token"] == "new-token"


def test_settings_and_ai_credentials_survive_restart(sources):
    session = Session(sources)
    config = AIConfig("https://provider.example/v1", "test-model", "first-key", 2, 45.0,
                      max_tool_rounds=12, max_tool_calls=3, sample_rows=20)
    session.update_settings(intellisense=False, panes={"history": False}, source="mysql", ai=config)
    restored = Session(sources)
    assert restored.intellisense is False
    assert restored.pane_visibility["history"] is False
    assert restored.pane_visibility["results"] is True
    assert restored.active_id == "mysql"
    assert restored.ai_config == config
    rotated = AIConfig("https://provider.example/v1", "other-model", "rotated-key", 4, 20.0,
                       max_tool_rounds=1, max_tool_calls=1, sample_rows=1)
    restored.update_settings(ai=rotated, dialect="duckdb")
    restarted = Session(sources)
    assert restarted.ai_config == rotated
    assert restarted.dialect == "duckdb"
    assert "first-key" not in restarted.settings_path.read_text(encoding="utf-8")


def test_legacy_ai_settings_migrate_and_fill_limits(sources, tmp_path):
    path = tmp_path / "settings.json"
    contents = json.dumps({"ai": {
        "base_url": "https://provider.example/v1", "model": "test-model",
        "api_key": "existing-key", "max_attempts": 2, "timeout": 45.0,
    }}).encode("utf-8")
    path.write_bytes(contents)
    session = Session(sources, data_dir=tmp_path)
    assert not session.memory_error
    assert session.ai_config.max_tool_rounds == 10
    assert session.ai_config.max_tool_calls == 16
    assert session.ai_config.sample_rows == 5
    assert session.ai_config.max_attempts == 2
    assert not session.ai_allow_sample_data
    assert not session.ai_test_queries
    assert session.ai_allow_schema_refreshes
    assert not session.ai_auto_accept
    assert b"existing-key" not in path.read_bytes()
    assert json.loads(path.read_bytes())["version"] == 2
    assert session.ai_config.api_key == "existing-key"
    assert Session(sources, data_dir=tmp_path).ai_config == session.ai_config
    assert "backups" in session.credential_warning


@pytest.mark.parametrize("contents", [
    b'{"secret-content":"secret-value","secret-content":"duplicate"}',
    b'{"ai":{"api_key":"secret-value"},',
    b'{"ai":{"base_url":"https://secret-value@example.com","model":"m"}}',
    b'{"ai":{"max_attempts":true}}',
    b'{"ai":{"max_tool_rounds":0}}',
    b'{"ai":{"max_tool_calls":17}}',
    b'{"ai":{"sample_rows":101}}',
    b'{"ai_allow_sample_data":"false"}',
    b'{"ai_allow_schema_refreshes":1}',
    b'{"ai_test_queries":1}',
    b'{"panes":{"secret-content":false}}',
    b'{"dialect":"secret-value"}',
    b'{"source":null}',
])
def test_invalid_settings_block_saves_without_echoing_secrets(sources, tmp_path, contents):
    path = tmp_path / "settings.json"
    path.write_bytes(contents)
    session = Session(sources, data_dir=tmp_path)
    assert session.memory_error
    assert "secret-value" not in session.memory_error
    assert "secret-content" not in session.memory_error
    with pytest.raises(ValueError) as failure:
        session.update_settings(intellisense=False)
    assert "secret-value" not in str(failure.value)
    assert "secret-content" not in str(failure.value)
    assert session.intellisense is True
    assert path.read_bytes() == contents


def test_duplicate_memory_keys_do_not_expose_secret_text(tmp_path):
    contents = b'{"secret-key":0,"secret-key":1}'
    path = tmp_path / "memory.json"
    path.write_bytes(contents)
    store = storage.MemoryStore(tmp_path)
    assert store.error
    assert "secret-key" not in store.error
    with pytest.raises(ValueError):
        store.save({}, [])
    assert path.read_bytes() == contents


def test_sparse_manually_edited_settings_are_used_without_rewriting(sources, tmp_path):
    path = tmp_path / "settings.json"
    contents = b'{"intellisense":false,"panes":{"sql":false}}\n'
    path.write_bytes(contents)
    session = Session(sources, data_dir=tmp_path)
    assert session.memory_error == ""
    assert session.intellisense is False
    assert session.pane_visibility["sql"] is False
    assert session.pane_visibility["results"] is True
    assert path.read_bytes() == contents
    session.update_settings(panes={"history": False})
    restored = Session(sources, data_dir=tmp_path)
    assert restored.pane_visibility["sql"] is False
    assert restored.pane_visibility["history"] is False


def test_external_settings_edits_are_not_overwritten(sources):
    session = Session(sources)
    session.update_settings(intellisense=False)
    contents = b'{"intellisense":true,"panes":{"history":false}}\n'
    session.settings_path.write_bytes(contents)
    with pytest.raises(ValueError, match="changed outside this session"):
        session.update_settings(panes={"sql": False})
    assert session.intellisense is False
    assert session.pane_visibility["sql"] is True
    assert session.settings_path.read_bytes() == contents
    restored = Session(sources)
    assert restored.intellisense is True
    assert restored.pane_visibility["history"] is False


def test_failed_settings_write_preserves_applied_state_and_credentials(sources, monkeypatch):
    session = Session(sources)
    original = AIConfig("https://provider.example/v1", "model", "original-secret")
    session.update_settings(ai=original, intellisense=False)
    before = session.settings_path.read_bytes()

    def deny_replace(source, destination):
        raise PermissionError("secret-from-filesystem")

    with monkeypatch.context() as patch:
        patch.setattr(storage.os, "replace", deny_replace)
        with pytest.raises(ValueError) as failure:
            session.update_settings(
                ai=AIConfig("https://other.example/v1", "other", "rotated-secret"),
                intellisense=True, panes={"sql": False}, source="mysql",
            )
        assert "secret" not in str(failure.value)
    assert session.ai_config == original
    assert session.intellisense is False
    assert session.pane_visibility["sql"] is True
    assert session.active_id == "demo"
    assert session.settings_path.read_bytes() == before
    assert Session(sources).ai_config == original
    session.update_settings(intellisense=True)
    assert Session(sources).intellisense is True


@pytest.mark.skipif(storage.os.name != "posix", reason="Windows permissions use filesystem ACLs")
def test_new_secret_files_and_directory_are_owner_only(tmp_path):
    directory = tmp_path / "private"
    memory = storage.MemoryStore(directory)
    settings = storage.SettingsStore(directory)
    memory.save({}, [{
        "account_id": "account", "database": DATABASE, "display": "Private", "api_token": "token",
    }])
    settings.save({"ai": {"base_url": "https://provider.example", "model": "m", "api_key": "key"}})
    assert directory.stat().st_mode & 0o777 == 0o700
    assert memory.path.stat().st_mode & 0o777 == 0o600
    assert settings.path.stat().st_mode & 0o777 == 0o600


def test_function_libraries_isolate_parser_completion_names_and_deletion(sources, tmp_path):
    session = Session(sources, data_dir=tmp_path)
    demo_body = "\\from events\n\\select 'demo' as origin\n\\limit 1"
    mysql_body = "\\from products\n\\select 'mysql' as origin\n\\limit 1"
    demo = session.save_fn("shared", "", demo_body, "Demo definition")
    session.save_fn("demo_only", "", demo_body, "")
    frozen = session.schema
    session.set_active("mysql")
    assert session.fns == {}
    assert parse_query("\\shared()", schema=session.schema, settled=True).errors
    assert not parse_query("\\shared()", schema=frozen, settled=True).errors
    line = "\\from demo_on"
    offered = view_for(session, line, line, len(line))
    assert offered is None or "demo_only()" not in offered.labels
    assert session.fn_call_rows() == []
    mysql = session.save_fn("shared", "", mysql_body, "MySQL definition")
    assert session.run("\\shared()", record=False).rows == [["mysql"]]
    session.set_active("demo")
    assert session.fns["shared"] == demo
    assert session.run("\\shared()", record=False).rows == [["demo"]]
    assert "demo_only()" in view_for(session, line, line, len(line)).labels
    session.delete_fn("shared")
    restored = Session(sources, data_dir=tmp_path)
    assert "shared" not in restored.fns
    restored.set_active("mysql")
    assert restored.fns == {"shared": mysql}
    assert restored.run("\\shared()", record=False).rows == [["mysql"]]


def test_temporary_function_targets_restore_schema_dialect_and_workspace(sources, tmp_path):
    session = Session(sources, data_dir=tmp_path)
    demo = session.save_fn("shared", "", "\\from events\n\\limit 1", "Demo")
    session.save_workspace(document="untouched draft", cursor=[0, 3])
    workspace = json.loads(json.dumps(session.workspace))
    session.dialect = "sqlite"
    with pytest.raises(RuntimeError, match="abandon target"):
        with session.target_source("mysql"):
            mysql = session.save_fn("shared", "", "\\from products\n\\limit 2", "MySQL")
            with session.target_source("demo"):
                assert session.fns == {"shared": demo}
            assert session.fns == {"shared": mysql}
            raise RuntimeError("abandon target")
    assert (session.active_id, session.dialect) == ("demo", "sqlite")
    assert session.fns == {"shared": demo}
    assert session.schema.fns == (demo,)
    assert session.workspace == workspace
    assert Session(sources, data_dir=tmp_path).functions_for("mysql") == {"shared": mysql}


@pytest.mark.parametrize("kind", ["d1", "postgres", "backend"])
def test_credential_writes_preserve_every_source_function_library(sources, tmp_path, kind):
    session = Session(sources, data_dir=tmp_path)
    demo = session.save_fn("shared", "", "\\from events\n\\limit 1", "Demo")
    with session.target_source("mysql"):
        mysql = session.save_fn("shared", "", "\\from products\n\\limit 2", "MySQL")
    if kind == "d1":
        session.remember_d1("account", DATABASE, "D1", "rotated-token")
    elif kind == "postgres":
        session.remember_postgres({
            "host": "localhost", "port": "5432", "database": "db", "user": "reader",
            "password": "rotated-secret", "schema": "public", "sslmode": "prefer", "display": "PG",
        })
    else:
        session.remember_backend({"backend": "sqlite", "options": {"database": ":memory:"}, "display": "SQLite"})
    restored = Session(sources, data_dir=tmp_path)
    assert restored.fns == {"shared": demo}
    assert restored.functions_for("mysql") == {"shared": mysql}


def test_function_library_survives_disconnected_target_reconnection(sources, tmp_path):
    source = replace(sources["demo"], id="old-label", kind="d1", dir=tmp_path / "saved.sqlite")
    session = Session({"demo": sources["demo"], source.id: source}, data_dir=tmp_path)
    session.set_active(source.id)
    saved = session.save_fn("local_events", "", "\\from events\n\\select 'snapshot' as origin\n\\limit 1", "Snapshot only")
    key = session.source_key()
    session.save_workspace(source=key, dialect="sqlite", document="\\local_events()")
    restored = Session({"demo": sources["demo"]}, data_dir=tmp_path)
    assert not restored.source_connected()
    assert restored.fns == {"local_events": saved}
    restored.register(replace(source, id="new-label"), activate=False)
    assert restored.source_key() == key
    assert restored.fns == {"local_events": saved}
    assert not restored.functions_for("demo")
    assert restored.run("\\local_events()", record=False).rows == [["snapshot"]]


@pytest.mark.parametrize("selection", ["settings", "workspace"])
def test_legacy_functions_belong_only_to_restored_selection(sources, tmp_path, selection):
    definition = {"name": "legacy", "params": [], "body": "\\from products\n\\limit 1", "description": "kept"}
    memory = tmp_path / "memory.json"
    contents = json.dumps({"version": 1, "functions": [definition], "d1_profiles": []}).encode()
    memory.write_bytes(contents)
    storage.SettingsStore(tmp_path).save({"source": "mysql"})
    owner = "mysql"
    if selection == "workspace":
        owner = f"saved-d1:account:{DATABASE}"
        workspace = storage.WorkspaceStore(tmp_path)
        workspace.save({**workspace.document, "source": owner, "dialect": "sqlite"})
    session = Session(sources, data_dir=tmp_path)
    assert session.source_key() == owner
    assert session.fns["legacy"].body == definition["body"]
    assert memory.read_bytes() == contents
    assert session.functions_for("demo") == {}
    if selection == "settings":
        session.update_settings(source="demo")
    else:
        session.set_active("demo")
        session.save_workspace(source="demo", dialect="duckdb", document="new draft")
    restored = Session(sources, data_dir=tmp_path)
    assert restored.source_key() == "demo"
    assert restored.fns == {}
    assert restored.functions_for(owner)["legacy"].body == definition["body"]
    assert json.loads(memory.read_bytes())["functions"][owner] == [definition]
