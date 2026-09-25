"""Durable workspace state preserves drafts without replaying database work."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from d8r import storage
from d8r.engine import DataSource
from d8r.engine.d1api import schema_connection
from d8r.tui.session import Session


def test_query_history_survives_restart_without_reexecuting(sources, tmp_path):
    session = Session(sources, data_dir=tmp_path)
    document = "\\from events\n\\select event_type\n\\limit 3"
    outcome = session.run(document)
    assert outcome.ok and len(outcome.rows) == 3
    session.save_workspace(document="\\from users\n\\limit 1", cursor=[1, 4])
    restored = Session(sources, data_dir=tmp_path)
    assert restored.history == session.history
    assert restored.history[0].doc == document
    assert restored.history[0].target == "demo"
    assert restored.workspace["document"] == "\\from users\n\\limit 1"
    assert restored.workspace["cursor"] == [1, 4]
    before = restored.workspace_path.read_bytes()
    Session(sources, data_dir=tmp_path)
    assert restored.workspace_path.read_bytes() == before
    assert "results" not in json.loads(before)


def test_workspace_history_and_editor_saves_merge_across_threads(sources, tmp_path):
    session = Session(sources, data_dir=tmp_path)
    with ThreadPoolExecutor(max_workers=1) as pool:
        execution = pool.submit(session.run, "\\from events\n\\limit 2")
        for draft in ("\\from", "\\from users", "\\from users\n\\limit 4"):
            session.save_workspace(document=draft)
        outcome = execution.result()
    assert outcome.ok
    restored = Session(sources, data_dir=tmp_path)
    assert restored.workspace["document"] == "\\from users\n\\limit 4"
    assert len(restored.history) == 1
    assert restored.history[0].rows == 2


def test_failed_history_save_keeps_successful_result_and_live_draft(sources, tmp_path, monkeypatch):
    session = Session(sources, data_dir=tmp_path)
    session.save_workspace(document="original")
    before = session.workspace_path.read_bytes()

    def deny_replace(*args):
        raise PermissionError("private filesystem details")

    with monkeypatch.context() as patch:
        patch.setattr(storage.os, "replace", deny_replace)
        outcome = session.run("\\from events\n\\limit 1")
    assert outcome.ok and len(outcome.rows) == 1
    assert session.workspace_error and "private filesystem details" not in outcome.status
    assert session.workspace_path.read_bytes() == before
    assert len(session.history) == 1
    session.save_workspace(document="latest unsaved draft")
    restored = Session(sources, data_dir=tmp_path)
    assert restored.workspace["document"] == "latest unsaved draft"
    assert len(restored.history) == 1


def test_stale_workspace_writer_never_overwrites_newer_draft(sources, tmp_path):
    first = Session(sources, data_dir=tmp_path)
    stale = Session(sources, data_dir=tmp_path)
    first.save_workspace(document="newer draft")
    before = first.workspace_path.read_bytes()
    with pytest.raises(ValueError):
        stale.save_workspace(document="stale draft")
    assert first.workspace_path.read_bytes() == before
    assert stale.workspace["document"] == "stale draft"
    assert Session(sources, data_dir=tmp_path).workspace["document"] == "newer draft"


def test_invalid_saved_chat_is_not_replayed_or_overwritten(sources, tmp_path):
    value = deepcopy(storage.WorkspaceStore(tmp_path).document)
    value["chats"]["target"] = {
        "messages": [{"role": "user", "content": "unfinished request"}],
        "transcript": "private draft text", "input": "", "turns": 1, "visible": True,
    }
    contents = json.dumps(value).encode()
    path = tmp_path / "workspace.json"
    path.write_bytes(contents)
    session = Session(sources, data_dir=tmp_path)
    assert session.workspace_error
    assert "private draft text" not in session.workspace_error
    assert session.load_chat("target") is None
    with pytest.raises(ValueError):
        session.save_workspace(document="replacement")
    assert path.read_bytes() == contents


def test_live_target_restores_disconnected_then_reconnects_by_database_identity(sources, tmp_path):
    client = SimpleNamespace(account_id="synthetic-account", database_uuid="11111111-2222-3333-4444-555555555555")
    con = schema_connection({"items": {"id": "!string"}})
    source = DataSource("old-slug", "Synthetic D1", "", "d1-live", "sqlite", Path("unused"), con=con, d1=client)
    session = Session({"demo": sources["demo"], source.id: source}, data_dir=tmp_path)
    key = session.source_key(source.id)
    session.save_workspace(document="\\from items", source=key, dialect="sqlite")
    restored = Session({"demo": sources["demo"]}, data_dir=tmp_path)
    assert restored.source_key() == key
    assert not restored.source_connected()
    assert "disconnected" in restored.source.display
    assert not restored.run("\\from items").ok
    assert restored.compile("\\from items")[0] is None
    connected = DataSource("new-slug", "Renamed D1", "", "d1-live", "sqlite", Path("unused"), con=con, d1=client)
    restored.register(connected, activate=False)
    assert restored.active_id == "new-slug"
    assert restored.source_key() == key
    assert restored.source_connected()
    assert key not in restored.sources


def test_snapshot_path_is_remembered_without_opening_it_on_restore(sources, tmp_path):
    path = tmp_path / "not-opened.sqlite"
    source = DataSource("snapshot", "Saved snapshot", "", "d1", "sqlite", path, con=sources["demo"].con)
    session = Session({"demo": sources["demo"], source.id: source}, data_dir=tmp_path)
    key = session.source_key(source.id)
    session.save_workspace(source=key, dialect="sqlite", document="\\from items")
    restored = Session({"demo": sources["demo"]}, data_dir=tmp_path)
    assert not path.exists()
    assert restored.saved_snapshot_path(restored.active_id) == str(path.resolve())
    assert not restored.source_connected()
