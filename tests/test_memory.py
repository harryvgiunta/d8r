"""Durable functions and token-free D1 profiles, including failed-write safety."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from anyql.ai.client import AIConfig
from anyql import storage
from anyql.tui.session import Session


DATABASE = "12345678-1234-5678-9abc-123456789abc"
BODY = "\\from events\n\\select @first as first, @second as second\n\\limit 2"


def test_functions_execute_after_restart_and_deletion_is_durable(sources):
    original = Session(sources)
    original.save_fn("pair", "first, second", BODY, "ordered arguments")
    original.save_fn("events_copy", "", "\\from events\n\\select user_id", "")
    original.remember_d1("account", DATABASE, "Production")

    restored = Session(sources)
    assert restored.fns["pair"].params == ["first", "second"]
    assert restored.fns["pair"].body == BODY
    assert restored.fns["pair"].doc == "ordered arguments"
    result = restored.run("\\from pair('hello', 42)\n\\select *")
    assert result.error == ""
    assert result.rows == [["hello", 42], ["hello", 42]]
    restored.delete_fn("pair")

    after_delete = Session(sources)
    assert list(after_delete.fns) == ["events_copy"]
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
    assert restored.fns["products_copy"].body == body
    restored.set_active("mysql")
    result = restored.run("\\from products_copy()\n\\select *")
    assert result.error == ""
    assert result.total == 2


@pytest.mark.parametrize("operation", ["save", "delete", "profile"])
def test_failed_replace_preserves_file_registry_and_profiles(sources, monkeypatch, operation):
    session = Session(sources)
    saved = session.save_fn("pair", "first, second", BODY, "original")
    session.remember_d1("account", DATABASE, "Original")
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
            session.remember_d1("account", DATABASE, "Changed")
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
    b'{"version":2,"functions":[],"d1_profiles":[]}',
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
        assert "access denied" in session.memory_error
        with pytest.raises(ValueError, match="restart"):
            session.save_fn("pair", "first, second", BODY, "")
    assert path.read_bytes() == contents


def test_d1_profiles_upsert_without_persisting_credentials(sources, monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "cloudflare-secret")
    session = Session(sources)
    session.ai_config = AIConfig(api_key="ai-secret")
    session.remember_d1("account", DATABASE, "Old label")
    session.remember_d1("account", DATABASE, "Production")
    expected = [{"account_id": "account", "database": DATABASE, "display": "Production"}]
    stored = json.loads(session.storage_path.read_text(encoding="utf-8"))
    assert stored == {"version": 1, "functions": [], "d1_profiles": expected}
    restored = Session(sources)
    assert restored.d1_profiles == expected
    assert restored.ai_config.api_key == ""
    assert all(source.kind != "d1-live" for source in restored.sources.values())


def test_profiles_require_resolved_uuid_and_preserve_previous_memory(sources):
    session = Session(sources)
    session.remember_d1("account", DATABASE, "Production")
    before = session.storage_path.read_bytes()
    with pytest.raises(ValueError, match="UUID"):
        session.remember_d1("account", "database-name", "Unresolved")
    assert session.storage_path.read_bytes() == before
    assert session.d1_profiles == [{"account_id": "account", "database": DATABASE, "display": "Production"}]


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
    assert Session(sources).fns == {}
    restored = Session(sources, data_dir=path)
    assert "pair" in restored.fns


@pytest.mark.parametrize("platform,variable,relative", [
    ("win32", "LOCALAPPDATA", "windows"),
    ("darwin", None, "Library/Application Support"),
    ("linux", "XDG_DATA_HOME", "xdg"),
])
def test_platform_data_directory(platform, variable, relative, tmp_path, monkeypatch):
    monkeypatch.delenv("ANYQL_DATA_DIR")
    monkeypatch.setattr(storage.sys, "platform", platform)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    if variable:
        monkeypatch.setenv(variable, str(tmp_path / relative))
    assert storage.data_directory() == tmp_path / relative / "anyql"
