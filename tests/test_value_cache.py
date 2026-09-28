"""On-demand distinct reads share typed, memory-only values across UI consumers."""
from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from d8r.engine import PayloadError, add_sqlite_source
from d8r.tui import session as session_module
from d8r.tui.session import Session


def make_source(tmp_path, name="fixture", *, source_id=None, rows=None):
    path = tmp_path / f"{name}.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("create table items (value text, number integer)")
        db.executemany(
            "insert into items values (?, ?)",
            [(None, None), ("NULL", 2), ("paid", 10), ("paid", 10)] if rows is None else rows,
        )
        db.execute("create table other (value text, number integer)")
        db.execute("insert into other values ('other', 42)")
    return add_sqlite_source(source_id or name, str(path))


def forbidden_read(*args, **kwargs):
    raise AssertionError("cache-only lookup must not execute a query")


def test_typed_values_are_shared_with_cache_only_qualified_completion(tmp_path, monkeypatch):
    source = make_source(tmp_path)
    session = Session({source.id: source}, data_dir=tmp_path / "state")
    doc = "\\from items i\n\\join other o on i.number = o.number"
    with monkeypatch.context() as patch:
        patch.setattr(session_module, "execute", forbidden_read)
        patch.setattr(session_module, "execute_remote", forbidden_read)
        assert session.cached_values(source, "items", "value") is None
        assert session.values_for(doc, "i.value") == []
        assert session.values_for(doc, "o.value") == []

    text_pool = session.distinct_values(source, "items", "value")
    number_pool = session.distinct_values(source, "items", "number")
    assert set(text_pool[0]) == {None, "NULL", "paid"} and not text_pool[1]
    assert set(number_pool[0]) == {None, 2, 10} and not number_pool[1]
    assert session.distinct_values(source, "other", "value") == (["other"], False)
    with monkeypatch.context() as patch:
        patch.setattr(session_module, "execute", forbidden_read)
        patch.setattr(session_module, "execute_remote", forbidden_read)
        assert session.distinct_values(source, "items", "number") == number_pool
        assert session.cached_values(source, "items", "value") == text_pool
        assert session.values_for(doc, "i.value") == ["NULL", "paid"]
        assert {float(value) for value in session.values_for(doc, "i.number")} == {2, 10}
        assert session.values_for(doc, "o.value") == ["other"]
        assert session.values_for(doc, "value") == ["NULL", "paid"]
        assert session.values_for(doc, "items.value") == []  # Aliases are strict.
        assert session.values_for(doc, "missing.value") == []
        session.refresh_schema()
        assert session.values_for(doc, "i.value") == ["NULL", "paid"]
    assert session.cached_value_columns == 3
    assert not session.history


def test_empty_success_is_cached_but_failure_is_retryable(tmp_path, monkeypatch):
    source = make_source(tmp_path, rows=[])
    session = Session({source.id: source}, data_dir=tmp_path / "state")
    real_execute = session_module.execute
    attempts = 0

    def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PayloadError("transient read failure")
        return real_execute(*args, **kwargs)

    monkeypatch.setattr(session_module, "execute", fail_once)
    with pytest.raises(PayloadError, match="transient read failure"):
        session.distinct_values(source, "items", "value")
    assert session.cached_values(source, "items", "value") is None
    assert session.cached_value_columns == 0
    assert session.values_for("\\from items", "value") == []
    assert attempts == 1
    assert session.distinct_values(source, "items", "value") == ([], False)
    monkeypatch.setattr(session_module, "execute", forbidden_read)
    assert session.cached_values(source, "items", "value") == ([], False)
    assert session.distinct_values(source, "items", "value") == ([], False)
    assert session.cached_value_columns == 1


def test_limit_clear_and_disable_control_retention_not_value_reads(tmp_path):
    source = make_source(tmp_path)
    session = Session({source.id: source}, data_dir=tmp_path / "state")
    session.update_settings(value_cache_limit=2)
    limited = session.distinct_values(source, "items", "number")
    assert len(limited[0]) == 2 and limited[1]
    epoch = session.value_cache_epoch
    session.update_settings(value_cache_limit=2, intellisense=False)
    assert session.value_cache_epoch == epoch
    assert session.cached_values(source, "items", "number") == limited

    session.update_settings(value_cache_limit=3)
    assert session.value_cache_epoch > epoch
    assert session.cached_values(source, "items", "number") is None
    complete = session.distinct_values(source, "items", "number")
    assert set(complete[0]) == {None, 2, 10} and not complete[1]
    epoch = session.value_cache_epoch
    session.clear_value_cache()
    assert session.value_cache_epoch > epoch
    assert session.cached_values(source, "items", "number") is None
    assert session.distinct_values(source, "items", "number") == complete

    session.update_settings(value_cache_enabled=False)
    assert session.cached_values(source, "items", "number") is None
    assert session.distinct_values(source, "items", "number") == complete
    with sqlite3.connect(source.dir) as db:
        db.execute("insert into items values ('fresh', 20)")
    refreshed = session.distinct_values(source, "items", "number")
    assert len(refreshed[0]) == 3 and refreshed[1]
    assert session.values_for("\\from items", "number") == []
    assert session.cached_value_columns == 0

    session.update_settings(value_cache_enabled=True, value_cache_limit=4)
    expected = session.distinct_values(source, "items", "number")
    assert set(expected[0]) == {None, 2, 10, 20} and not expected[1]
    assert session.cached_values(source, "items", "number") == expected
    restored = Session({source.id: source}, data_dir=tmp_path / "state")
    assert restored.value_cache_enabled and restored.value_cache_limit == 4
    assert restored.cached_values(source, "items", "number") is None


def test_source_identity_switches_and_selective_invalidation(tmp_path, monkeypatch):
    first = make_source(tmp_path, "first", rows=[("first", 1)])
    second = make_source(tmp_path, "second", rows=[("second", 2)])
    replacement = make_source(tmp_path, "replacement", source_id=first.id, rows=[("replacement", 3)])
    session = Session({first.id: first, second.id: second}, data_dir=tmp_path / "state")
    assert session.distinct_values(first, "items", "value") == (["first"], False)
    assert session.cached_values(replacement, "items", "value") is None
    assert session.distinct_values(replacement, "items", "value") == (["replacement"], False)
    assert session.distinct_values(second, "items", "value") == (["second"], False)
    epoch = session.value_cache_epoch
    with monkeypatch.context() as patch:
        patch.setattr(session_module, "execute", forbidden_read)
        session.set_active(second.id)
        assert session.values_for("\\from items", "value") == ["second"]
        session.set_active(first.id)
        assert session.values_for("\\from items", "value") == ["first"]
        assert session.value_cache_epoch == epoch

    session.register(replacement)
    assert session.value_cache_epoch > epoch
    assert session.cached_values(first, "items", "value") is None
    assert session.cached_values(replacement, "items", "value") is None
    assert session.cached_values(second, "items", "value") == (["second"], False)
    assert session.distinct_values(replacement, "items", "value") == (["replacement"], False)
    session.clear_value_cache(second.id)
    assert session.cached_values(second, "items", "value") is None
    assert session.cached_values(replacement, "items", "value") == (["replacement"], False)


def test_queued_stale_read_is_rejected_before_io_or_a_new_cache_hit(tmp_path, monkeypatch):
    source = make_source(tmp_path)
    session = Session({source.id: source}, data_dir=tmp_path / "state")
    scheduled_epoch = session.value_cache_epoch
    session.clear_value_cache()
    with monkeypatch.context() as patch:
        patch.setattr(session_module, "execute", forbidden_read)
        with pytest.raises(PayloadError):
            session.distinct_values(source, "items", "number", epoch=scheduled_epoch)
    assert session.cached_values(source, "items", "number") is None

    current = session.distinct_values(source, "items", "number", epoch=session.value_cache_epoch)
    assert set(current[0]) == {None, 2, 10} and not current[1]
    with monkeypatch.context() as patch:
        patch.setattr(session_module, "execute", forbidden_read)
        with pytest.raises(PayloadError):
            session.distinct_values(source, "items", "number", epoch=scheduled_epoch)
    assert session.cached_values(source, "items", "number") == current


@pytest.mark.parametrize("change", ["clear", "disable", "limit", "replace"])
def test_inflight_reads_keep_their_bound_but_cannot_repopulate_after_invalidation(
    tmp_path, monkeypatch, change,
):
    source = make_source(tmp_path)
    replacement = make_source(tmp_path, "replacement", source_id=source.id)
    session = Session({source.id: source}, data_dir=tmp_path / "state")
    session.update_settings(value_cache_limit=2)
    entered = Event()
    release = Event()

    def delayed_read(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return {"rows": [[10], [20], [30]]}

    def invalidate():
        if change == "clear":
            session.clear_value_cache()
        elif change == "disable":
            session.update_settings(value_cache_enabled=False)
        elif change == "limit":
            session.update_settings(value_cache_limit=1)
        else:
            session.register(replacement, activate=False)

    monkeypatch.setattr(session_module, "execute", delayed_read)
    with ThreadPoolExecutor(max_workers=2) as workers:
        reading = workers.submit(
            session.distinct_values, source, "items", "number", epoch=session.value_cache_epoch,
        )
        try:
            assert entered.wait(5)
            # Invalidation must complete while the engine request remains blocked.
            workers.submit(invalidate).result(timeout=5)
        finally:
            release.set()
        assert reading.result(timeout=5) == ([10, 20], True)
    assert session.cached_values(source, "items", "number") is None
    assert session.cached_values(replacement, "items", "number") is None
    assert session.cached_value_columns == 0


def test_temp_replacement_drop_and_rollback_invalidate_values(tmp_path):
    source = make_source(tmp_path)
    session = Session({source.id: source}, data_dir=tmp_path / "state")
    session.distinct_values(source, "items", "value")
    result = session.run("\\from items\n\\select number\n\\where number = 2\n\\temp kept")
    assert result.ok, result.error
    assert session.cached_values(source, "items", "value") is None
    assert session.distinct_values(source, "kept", "number") == ([2], False)

    result = session.run("\\from items\n\\select number\n\\where number = 10\n\\temp kept")
    assert result.ok, result.error
    assert session.cached_values(source, "kept", "number") is None
    assert session.distinct_values(source, "kept", "number") == ([10], False)
    session.apply_tx("begin", None)
    session.drop_temp_table("kept")
    assert session.cached_values(source, "kept", "number") is None
    session.apply_tx("rollback", None)
    assert session.distinct_values(source, "kept", "number") == ([10], False)

    session.apply_tx("begin", None)
    source.con.raw_sql("UPDATE kept SET number = 99")
    session.clear_value_cache(source.id)
    assert session.distinct_values(source, "kept", "number") == ([99], False)
    session.apply_tx("rollback", None)
    assert session.cached_values(source, "kept", "number") is None
    assert session.distinct_values(source, "kept", "number") == ([10], False)
