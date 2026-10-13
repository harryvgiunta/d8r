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


def _function_draft(source, name):
    return {"identity": f"{source}-draft", "source": source, "selected": "", "name": name,
            "description": "unsaved", "parameters": "n", "body": "\\from products\n\\limit @n",
            "arguments": "2", "cursor": [1, 4]}


def test_source_workspaces_keep_independent_documents_drafts_and_views(sources, tmp_path):
    session = Session(sources, data_dir=tmp_path)
    demo_draft = _function_draft("demo", "demo_copy")
    mysql_draft = _function_draft("mysql", "products_copy")
    session.save_workspace(document_id="demo-page", document="demo query", source="demo",
                           function_draft=demo_draft, active_view="function")
    session.set_active("mysql")
    session.save_workspace(document_id="mysql-page", document="mysql query", source="mysql",
                           function_draft=mysql_draft, active_view="workspace")
    session.set_active("demo")
    session.save_workspace(document_id="demo-page", document="revised demo query", source="demo",
                           function_draft=None, active_view="workspace")
    restored = Session(sources, data_dir=tmp_path)
    assert restored.workspace["source_workspaces"] == {
        "demo": {"document_id": "demo-page", "function_draft": None, "active_view": "workspace"},
        "mysql": {"document_id": "mysql-page", "function_draft": mysql_draft, "active_view": "workspace"},
    }
    assert {page["source"]: page["document"] for page in restored.workspace["pages"]} == {
        "demo": "revised demo query", "mysql": "mysql query",
    }


def test_legacy_cross_source_function_draft_restores_only_in_its_workspace(sources, tmp_path):
    workspace = storage.WorkspaceStore(tmp_path)
    draft = _function_draft("mysql", "products_copy")
    legacy = {**workspace.document, "source": "demo", "document_id": "demo-page",
              "document": "demo query", "function_draft": draft, "active_view": "function"}
    del legacy["source_workspaces"]
    contents = json.dumps(legacy).encode()
    workspace.path.write_bytes(contents)
    restored = Session(sources, data_dir=tmp_path)
    assert restored.source_key() == "demo"
    assert restored.workspace["document"] == "demo query"
    assert restored.workspace["function_draft"] is None
    assert restored.workspace["active_view"] == "workspace"
    assert restored.workspace["source_workspaces"] == {
        "demo": {"document_id": "demo-page", "function_draft": None, "active_view": "workspace"},
        "mysql": {"document_id": "", "function_draft": draft, "active_view": "function"},
    }
    assert workspace.path.read_bytes() == contents
    restored.save_workspace()
    assert Session(sources, data_dir=tmp_path).workspace["source_workspaces"]["mysql"]["function_draft"] == draft


def test_function_draft_save_uses_its_target_not_the_query_source(sources, tmp_path):
    session = Session(sources, data_dir=tmp_path)
    draft = _function_draft("mysql", "products_copy")
    session.save_workspace(document="demo query", source="demo", function_draft=draft, active_view="function")
    assert session.workspace["source_workspaces"]["demo"]["function_draft"] is None
    assert session.workspace["source_workspaces"]["mysql"]["function_draft"] == draft
    restored = Session(sources, data_dir=tmp_path)
    assert restored.workspace["function_draft"] is None
    assert restored.workspace["source_workspaces"]["mysql"]["function_draft"] == draft


@pytest.mark.parametrize("states", [
    [],
    {"": {"document_id": "", "function_draft": None, "active_view": "workspace"}},
    {"demo": {"document_id": "page", "active_view": "workspace"}},
    {"demo": {"document_id": 1, "function_draft": None, "active_view": "workspace"}},
    {"demo": {"document_id": "page", "function_draft": None, "active_view": "execute"}},
    {"demo": {"document_id": "page", "function_draft": {"private": "secret"}, "active_view": "function"}},
    {"demo": {"document_id": "page", "function_draft": _function_draft("mysql", "wrong"), "active_view": "function"}},
])
def test_invalid_source_workspaces_are_not_overwritten(sources, tmp_path, states):
    workspace = storage.WorkspaceStore(tmp_path)
    contents = json.dumps({**workspace.document, "source_workspaces": states}).encode()
    workspace.path.write_bytes(contents)
    session = Session(sources, data_dir=tmp_path)
    assert session.workspace_error
    assert "secret" not in session.workspace_error
    with pytest.raises(ValueError):
        session.save_workspace(document="new draft")
    assert workspace.path.read_bytes() == contents
