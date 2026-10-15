"""Connection isolation applies to headless reads, saves, and durable restarts."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from d8r.ai.context import AIContext
from d8r.storage import WorkspaceStore
from d8r.tui.session import Session


DEMO_QUERY = "\\from events\n\\select event_type\n\\limit 2"
MYSQL_QUERY = "\\from products\n\\limit 1"


def test_history_and_completion_restore_only_the_selected_connection(sources, tmp_path):
    session = Session(sources, data_dir=tmp_path)
    assert session.run(DEMO_QUERY).ok
    assert session.completion_ranks("", category="dataset") == {"events": 0}
    session.set_active("mysql")
    assert session.history == []
    assert session.completion_ranks("", category="dataset") == {}
    assert session.run(MYSQL_QUERY).ok
    session.set_active("demo")
    recent = "\\from users\n\\limit 1"
    assert session.run(recent).ok
    assert [entry.doc for entry in session.history] == [recent, DEMO_QUERY]
    assert session.completion_ranks("", category="dataset") == {"users": 0, "events": 1}
    session.save_workspace(document="unsaved demo draft", source="demo")

    restored = Session(sources, data_dir=tmp_path)
    assert [entry.doc for entry in restored.history] == [recent, DEMO_QUERY]
    with restored.target_source("mysql"):
        assert [entry.doc for entry in restored.history] == [MYSQL_QUERY]
        assert restored.completion_ranks("", category="dataset") == {"products": 0}
    assert [entry.doc for entry in restored.history] == [recent, DEMO_QUERY]
    assert [entry["doc"] for entry in restored.workspace["history"]] == [recent, MYSQL_QUERY, DEMO_QUERY]


def test_failed_history_save_preserves_other_connections_on_retry(sources, tmp_path, monkeypatch):
    session = Session(sources, data_dir=tmp_path)
    assert session.run(DEMO_QUERY).ok
    session.set_active("mysql")

    def deny_save(document):
        raise ValueError("Workspace unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(session._workspace, "save", deny_save)
        outcome = session.run(MYSQL_QUERY)
    assert outcome.ok and "history not saved" in outcome.status
    assert [entry.doc for entry in session.history] == [MYSQL_QUERY]
    session.set_active("demo")
    assert [entry.doc for entry in session.history] == [DEMO_QUERY]
    session.save_workspace(document="retry from another connection")
    restored = Session(sources, data_dir=tmp_path)
    assert [entry.doc for entry in restored.history] == [DEMO_QUERY]
    restored.set_active("mysql")
    assert [entry.doc for entry in restored.history] == [MYSQL_QUERY]


def test_legacy_history_uses_saved_identity_after_slug_reuse_and_reconnect(sources, tmp_path):
    path = tmp_path / "saved.sqlite"
    key = f"snapshot:{path.resolve()}"
    store = WorkspaceStore(tmp_path)
    legacy = {
        **store.document,
        "source": "demo",
        "sources": {key: {"id": "demo", "display": "Previous connection", "kind": "d1", "path": str(path)}},
        "history": [{"at": "then", "source": "demo", "dialect": "duckdb", "rows": 2,
                     "ms": 1, "doc": DEMO_QUERY}],
    }
    contents = json.dumps(legacy).encode()
    store.path.write_bytes(contents)
    session = Session(sources, data_dir=tmp_path)
    assert session.history == []
    assert session.workspace_path.read_bytes() == contents
    session.restore_source(key)
    assert not session.source_connected()
    assert [entry.doc for entry in session.history] == [DEMO_QUERY]
    connected = replace(sources["demo"], id="renamed", kind="d1", dir=path)
    session.register(connected, activate=False)
    assert session.source_key() == key
    assert [entry.doc for entry in session.history] == [DEMO_QUERY]
    session.save_workspace(source=key, dialect="duckdb", document="snapshot draft")
    restarted = Session(sources, data_dir=tmp_path)
    assert restarted.source_key() == key and not restarted.source_connected()
    assert [entry.doc for entry in restarted.history] == [DEMO_QUERY]
    restarted.set_active("demo")
    assert restarted.history == []


def test_ai_reads_and_saves_use_captured_connection_not_active_workspace(sources, tmp_path):
    async def scenario():
        session = Session(sources, data_dir=tmp_path)
        demo = session.save_fn("shared", "", DEMO_QUERY, "Demo only")
        assert session.run(DEMO_QUERY).ok
        context = AIContext(session, "demo", "", save_guard=lambda: None)
        session.set_active("mysql")
        mysql = session.save_fn("shared", "", MYSQL_QUERY, "MySQL only")
        session.save_fn("mysql_only", "", MYSQL_QUERY, "Private to MySQL")
        assert session.run(MYSQL_QUERY).ok
        session.save_workspace(source="mysql", document="untouched mysql draft")
        workspace = json.loads(json.dumps(session.workspace))

        history = json.loads(await context.call_tool("query_history", {}))
        assert [entry["document"] for entry in history["history"]] == [DEMO_QUERY]
        functions = json.loads(await context.call_tool("functions", {}))["functions"]
        assert [(fn["name"], fn["body"]) for fn in functions] == [("shared", demo.body)]
        invalid = json.loads(await context.call_tool("validate_d8r", {"text": "\\from mysql_only()"}))
        assert invalid["valid"] is False
        with pytest.raises(ValueError):
            with session.target_source("demo"):
                session.save_fn("cross_connection", "", "\\from mysql_only()", "")
        updated = "\\from users\n\\limit 1"
        saved = json.loads(await context.call_tool("save_function", {
            "name": "shared", "original_name": "shared", "parameters": [],
            "description": "Updated demo only", "body": updated,
        }))
        assert saved == {"saved": True, "name": "shared"}
        assert session.active_id == "mysql"
        assert session.fns["shared"] == mysql
        assert [entry.doc for entry in session.history] == [MYSQL_QUERY]
        assert session.workspace == workspace
        restored = Session(sources, data_dir=tmp_path)
        assert restored.fns["shared"] == mysql
        restored.set_active("demo")
        assert restored.fns["shared"].body == updated
        assert "mysql_only" not in restored.fns
        assert restored.run("\\shared()", record=False).total == 1

    asyncio.run(scenario())


def test_connection_identity_survives_disconnect_and_saved_slug_changes(sources, tmp_path):
    source = replace(sources["demo"], id="old-name", kind="d1", dir=tmp_path / "saved.sqlite")
    session = Session({"demo": sources["demo"], source.id: source}, data_dir=tmp_path)
    session.update_settings(source=source.id)
    saved = session.save_fn("owned", "", DEMO_QUERY, "Snapshot only")
    key = session.source_key()
    assert session.restore_source(source.id) == source.id
    assert session.source is source and session.source_connected()
    session.register(replace(source, con=None), activate=False)
    assert not session.source_connected()
    assert session.source_key() == key and session.fns == {"owned": saved}

    renamed = replace(source, id="new-name")
    restored = Session({"demo": sources["demo"], renamed.id: renamed}, data_dir=tmp_path)
    assert restored.source is renamed
    assert restored.source_key() == key
    assert restored.fns == {"owned": saved}
    restored.set_active("demo")
    assert restored.fns == {}
