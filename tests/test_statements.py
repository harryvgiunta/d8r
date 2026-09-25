"""Temp tables and transactions: the two things a document writes.

Both are session state, so these tests drive a real `Session` (the same headless
core the app uses) against a fresh registry, and assert what a person would see:
which tables exist, what the status says, and — the point of a transaction —
that rolling back takes a `\\temp` with it because temp-table DDL is
transactional. The capabilities that gate each engine are asserted per source,
including where they are refused.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import ibis
import pytest

from anyql.engine import DataSource, add_sqlite_source, capabilities_for, datasources, expression, tx
from anyql.tui.session import Session
from tests.conftest import DATA_DIR


@pytest.fixture()
def session() -> Session:
    """A session on its own registry: temp tables never leak into other tests."""
    return Session(datasources.load(DATA_DIR))


@pytest.fixture()
def snapshot(tmp_path) -> DataSource:
    """A real SQLite snapshot source — the engine that has savepoints."""
    path = tmp_path / "snapshot.sqlite"
    raw = sqlite3.connect(path)
    raw.executescript("create table t(a integer); insert into t values (1), (2), (3);")
    raw.commit()
    raw.close()
    return add_sqlite_source("snap", str(path))


@pytest.fixture(params=["sqlite", "duckdb"])
def temp_backend(request, tmp_path):
    """Independent synthetic databases, including a persistent name collision."""
    if request.param == "sqlite":
        con = ibis.sqlite.connect(tmp_path / "temp-isolation.sqlite")
        con.con.isolation_level = None
        persistent = '"main"'
        temporary = '"temp"'
    else:
        con = ibis.duckdb.connect(tmp_path / "temp-isolation.duckdb")
        persistent = f'"{con.current_catalog}"."main"'
        temporary = '"temp"."main"'
    try:
        con.con.execute("CREATE TABLE t (a INTEGER)")
        con.con.execute("INSERT INTO t VALUES (1), (2), (3)")
        con.con.execute("CREATE TABLE collision (a INTEGER)")
        con.con.execute("INSERT INTO collision VALUES (99)")
        yield con, persistent, temporary
    finally:
        con.con.close()


def live_source() -> DataSource:
    """A source shaped like a live D1: it answers over HTTP, so it is *not* local."""
    con = ibis.duckdb.connect()
    con.create_table("t", ibis.memtable({"a": [1, 2]}))
    source = DataSource(
        id="d1-probe",
        display="D1 · probe",
        doc="",
        kind="d1-live",
        dialect="sqlite",
        dir=Path("probe"),
        con=con,
        d1=SimpleNamespace(account_id="synthetic", database_uuid="11111111-2222-3333-4444-555555555555"),
    )
    source.datasets = {"t": {"table": con.table("t"), "doc": "", "rows": 2}}
    return source


# --- temp tables -------------------------------------------------------------


def test_temp_keeps_the_query_and_shows_its_rows(session):
    outcome = session.run("\\from events\n\\select event_type\n\\select count(user_id) as n\n\\group event_type\n\\temp spread\n")
    assert outcome.ok and outcome.error == ""
    assert outcome.rows, "the run still answers with the rows the query asked for"
    assert outcome.schema_changed is True
    assert outcome.status.startswith('created temp table "spread" · ')
    assert session.temp_tables() == ["spread"]
    assert session.source.datasets["spread"]["temp"] is True
    assert session.source.datasets["spread"]["rows"] == len(outcome.rows)
    # The table is a table now: the parser's registry and a later document see it.
    assert session.schema.table_by_name("spread") is not None
    later = session.run("\\from spread\n\\select event_type\n\\order event_type\n\\limit 2")
    assert later.ok and [row[0] for row in later.rows] == ["click", "purchase"]


def test_temp_rewrites_on_a_second_run(session):
    doc = "\\from users\n\\select user_id\n\\limit 3\n\\temp few\n"
    session.run(doc)
    assert session.source.datasets["few"]["rows"] == 3
    session.run("\\from users\n\\select user_id\n\\limit 5\n\\temp few\n")
    assert session.source.datasets["few"]["rows"] == 5


def test_drop_removes_a_temp_table_and_only_temp_tables(session):
    session.run("\\from users\n\\select user_id\n\\limit 2\n\\temp doomed\n")
    outcome = session.run("\\drop doomed\n")
    assert outcome.ok and outcome.status == 'dropped temp table "doomed"'
    assert session.temp_tables() == []
    assert session.schema.table_by_name("doomed") is None

    # A bundled dataset is not the session's to drop, and neither is a stranger.
    assert "is not a temp table" in session.run("\\drop events\n").error
    assert "is not a temp table" in session.run("\\drop ghost\n").error
    assert "events" in session.source.datasets


def test_temp_refuses_a_name_that_is_already_a_dataset(session):
    assert session.run("\\from events\n\\temp events\n").error == (
        '"events" is already a dataset — pick another temp name'
    )


def test_temp_needs_a_query(session):
    assert session.run("\\temp orphan\n").error == (
        "\\temp needs a query to keep — this document has no \\from"
    )


def test_compile_tells_a_temp_document_what_running_it_does(session):
    sql, message = session.compile("\\from events\n\\select user_id\n\\temp keep\n")
    assert sql is not None and "SELECT" in sql
    assert message.endswith("· \\temp materializes on run")
    assert session.compile("\\begin\n")[1] == "nothing to compile — this document carries statements only"


def test_a_live_source_can_hold_neither_a_temp_table_nor_a_transaction():
    session = Session({"d1-probe": live_source()})
    assert session.run("\\begin\n").error.startswith("a live D1 source has no transactions")
    assert session.run("\\from t\n\\temp kept\n").error == (
        "a live D1 source cannot hold a temp table — it is reached over HTTP"
    )



@pytest.mark.parametrize("name", ["_cf_KV", "_CF_kv", "sqlite_sequence", "SQLITE_SEQUENCE"])
def test_reserved_temp_names_preserve_hidden_sqlite_tables(tmp_path, name):
    path = tmp_path / "hidden-tables.sqlite"
    with sqlite3.connect(path) as raw:
        raw.executescript(
            "CREATE TABLE t (a INTEGER PRIMARY KEY AUTOINCREMENT);"
            "INSERT INTO t VALUES (7);"
            "CREATE TABLE _cf_KV (key TEXT PRIMARY KEY, value TEXT);"
            "INSERT INTO _cf_KV VALUES ('synthetic-key', 'synthetic-value');"
        )
    raw.close()
    source = add_sqlite_source("snap", str(path))
    try:
        snap = Session({"snap": source})
        assert set(source.datasets) == {"t"}
        outcome = snap.run(f"\\from t\n\\temp {name}\n")
        assert not outcome.ok and "reserved" in outcome.error
        assert snap.temp_tables() == []
        assert source.con.con.execute("SELECT * FROM main._cf_KV").fetchall() == [
            ("synthetic-key", "synthetic-value")
        ]
        assert source.con.con.execute("SELECT * FROM main.sqlite_sequence").fetchall() == [("t", 7)]
        source.con.con.execute("INSERT INTO main.t DEFAULT VALUES")
        assert source.con.con.execute("SELECT a FROM main.t ORDER BY a").fetchall() == [(7,), (8,)]
    finally:
        source.con.con.close()


def test_reserved_temp_rejection_preserves_existing_temp_and_transaction(temp_backend):
    con, _, temporary = temp_backend
    expr = con.table("t")
    con.con.execute(f'CREATE TEMPORARY TABLE {temporary}."_cF_private" AS SELECT 42 AS a')
    tx.begin(con)
    try:
        with pytest.raises(expression.PayloadError, match="reserved"):
            tx.create_temp(con, "_cF_private", expr)
        assert con.con.execute(f'SELECT a FROM {temporary}."_cF_private"').fetchall() == [(42,)]
        tx.create_temp(con, "inside", expr)
        tx.rollback(con)
        # The failed reserved-name attempt neither commits nor aborts our transaction.
        with pytest.raises(Exception, match="inside"):
            con.con.execute(f'SELECT a FROM {temporary}."inside"')
        assert con.con.execute(f'SELECT a FROM {temporary}."_cF_private"').fetchall() == [(42,)]
    finally:
        tx.drop_temp(con, "_cF_private")


def test_temp_replacement_preserves_persistent_collision_and_rolls_back(temp_backend):
    con, persistent, temporary = temp_backend
    expr = con.table("t")
    tx.create_temp(con, "collision", expr.filter(expr.a == 1))
    assert con.con.execute(f'SELECT a FROM {persistent}."collision"').fetchall() == [(99,)]
    tx.begin(con)
    tx.create_temp(con, "collision", expr.filter(expr.a > 1))
    assert con.con.execute(f'SELECT a FROM {temporary}."collision" ORDER BY a').fetchall() == [(2,), (3,)]
    tx.rollback(con)
    assert con.con.execute(f'SELECT a FROM {temporary}."collision"').fetchall() == [(1,)]
    assert con.con.execute(f'SELECT a FROM {persistent}."collision"').fetchall() == [(99,)]


def test_drop_temp_never_falls_back_to_persistent_collision(temp_backend):
    con, persistent, temporary = temp_backend
    expr = con.table("t")
    tx.drop_temp(con, "collision")
    assert con.con.execute(f'SELECT a FROM {persistent}."collision"').fetchall() == [(99,)]
    tx.create_temp(con, "collision", expr.filter(expr.a == 2))
    tx.begin(con)
    tx.drop_temp(con, "collision")
    assert con.con.execute(f'SELECT a FROM {persistent}."collision"').fetchall() == [(99,)]
    tx.rollback(con)
    assert con.con.execute(f'SELECT a FROM {temporary}."collision"').fetchall() == [(2,)]
    tx.drop_temp(con, "collision")
    tx.drop_temp(con, "collision")
    assert con.con.execute('SELECT a FROM "collision"').fetchall() == [(99,)]

# --- transactions ------------------------------------------------------------


def test_a_transaction_runs_as_a_statement_and_reports_itself(session):
    outcome = session.run("\\begin\n")
    assert outcome.ok and outcome.status == "transaction open · demo"
    assert outcome.rows == [] and outcome.error == ""
    assert session.tx_state().open is True
    assert session.history[0].doc == "\\begin\n"

    assert session.run("\\begin\n").error == (
        "a transaction is already open on demo — \\commit or \\rollback first"
    )
    assert session.run("\\commit\n").status == "transaction committed · demo"
    assert session.tx_state().open is False
    assert session.run("\\commit\n").error == "no transaction is open on demo — \\begin first"
    assert session.run("\\rollback\n").error == "no transaction is open on demo — \\begin first"


def test_a_query_runs_inside_the_statement_it_is_written_with(session):
    session.run("\\begin\n")
    outcome = session.run("\\from users\n\\select user_id\n\\limit 2")
    assert outcome.ok and len(outcome.rows) == 2
    session.run("\\rollback\n")
    assert session.tx_state().open is False


def test_rolling_back_takes_the_temp_table_with_it(session):
    session.run("\\begin\n")
    session.run("\\from events\n\\select user_id\n\\temp inside\n")
    assert session.temp_tables() == ["inside"]

    outcome = session.run("\\rollback\n")
    assert outcome.status == "transaction rolled back · demo"
    # The engine rolled the DDL back, and the session's registry followed.
    assert session.temp_tables() == []
    assert session.schema.table_by_name("inside") is None
    assert session.run("\\from inside\n\\select user_id\n").error.startswith("line 1: unknown table")


def test_commit_keeps_what_the_transaction_wrote(session):
    session.run("\\begin\n")
    session.run("\\from events\n\\select user_id\n\\temp kept\n")
    session.run("\\commit\n")
    assert session.temp_tables() == ["kept"]
    assert session.run("\\from kept\n\\select user_id\n\\limit 2").ok


def test_a_rolled_back_drop_puts_the_temp_table_back(session):
    session.run("\\from events\n\\select user_id\n\\temp kept\n")
    session.run("\\begin\n")
    assert session.run("\\drop kept\n").status == 'dropped temp table "kept"'
    assert session.temp_tables() == []

    session.run("\\rollback\n")
    # The engine put the table back, and the registry followed it there.
    assert session.temp_tables() == ["kept"]
    assert session.source.datasets["kept"]["rows"] == 100
    assert session.run("\\from kept\n\\select user_id\n\\limit 2").ok


def test_savepoints_are_the_engines_that_have_them(session, snapshot, tmp_path):
    # DuckDB keeps whole transactions only, and says so.
    session.run("\\begin\n")
    assert session.run("\\savepoint s1\n").error == (
        "the duckdb engine has no savepoints — it keeps whole transactions "
        "(\\begin, \\commit, \\rollback)"
    )

    # A SQLite snapshot has them: back to a savepoint keeps the earlier table.
    snap = Session({"snap": snapshot}, data_dir=tmp_path / "sqlite-workspace")
    assert snap.run("\\begin\n").ok
    assert snap.run("\\savepoint before\n").status == 'savepoint "before" set'
    assert snap.run("\\from t\n\\select a\n\\temp second\n").ok
    assert snap.run("\\rollback to before\n").status == 'rolled back to savepoint "before"'
    assert snap.temp_tables() == []
    assert snap.tx_state().open is True and snap.tx_state().savepoints == ["before"]
    assert snap.run("\\release before\n").status == 'savepoint "before" released'
    assert snap.tx_state().savepoints == []
    assert snap.run("\\commit\n").ok


def test_a_savepoint_names_what_is_open(session, snapshot):
    snap = Session({"snap": snapshot})
    assert snap.run("\\savepoint early\n").error == "no transaction is open on snap — \\begin first"
    snap.run("\\begin\n")
    snap.run("\\savepoint one\n")
    assert snap.run("\\rollback to two\n").error == 'unknown savepoint "two" — open: one'
    assert snap.run("\\savepoint one\n").error == 'savepoint "one" already exists'


def test_release_drops_the_savepoints_after_it(session, snapshot):
    snap = Session({"snap": snapshot})
    snap.run("\\begin\n")
    snap.run("\\savepoint a\n")
    snap.run("\\savepoint b\n")
    snap.run("\\release a\n")
    assert snap.tx_state().savepoints == []
    assert snap.run("\\rollback to b\n").error == 'unknown savepoint "b" — open: (none)'


def test_the_engine_seam_names_its_engine(con):
    assert tx.engine_of(con) == "duckdb"
    with pytest.raises(expression.PayloadError, match="has no savepoints"):
        tx.savepoint(con, "s1")
    # Dropping is idempotent at the seam — the session is what refuses a name
    # that is not a temp table of its own.
    tx.drop_temp(con, "never_created")


def test_capabilities_say_what_each_source_can_do():
    demo = capabilities_for(datasources.load(DATA_DIR)["demo"])["supports"]
    assert demo["temp"] is True and demo["transactions"] is True
    assert demo["savepoints"] is False  # DuckDB keeps whole transactions only
    assert demo["subquery"] and demo["lateral"] and demo["frame"] and demo["regex"]
    assert capabilities_for(live_source())["supports"] == {
        **demo,
        "savepoints": False,
        "temp": False,
        "transactions": False,
        "regex": False,  # `~`/`!~` compile to `_IBIS_REGEX_SEARCH`; D1 has no UDFs
    }
