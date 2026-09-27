"""PostgreSQL safety edges and opt-in integration against a disposable test schema.

Set D8R_TEST_POSTGRES=1 and PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD to run
against an explicitly chosen test database. The role needs CREATE SCHEMA;
UUID-named schemas are removed afterwards. The default offline suite never
connects to PostgreSQL or requires Docker.
"""

from __future__ import annotations

import os
import traceback
import uuid
from pathlib import Path
from types import SimpleNamespace

import ibis
import pytest

from d8r.engine import (
    DataSource,
    PayloadError,
    add_postgres_source,
    capabilities_for,
    execute,
    tx,
)


SECRET = "distinctive-password-do-not-print"
CONNECT = dict(host="localhost", database="app", user="reader", password=SECRET)


@pytest.mark.parametrize("changed", [
    {"port": 0},
    {"port": 65536},
    {"port": 5432.5},
    {"port": True},
    {"sslmode": "silently-ignore-tls"},
    {"schema": "pg_catalog"},
    {"host": f"postgresql://reader:{SECRET}@localhost/app"},
])
def test_invalid_connection_settings_fail_before_connecting(changed, monkeypatch):
    psycopg = pytest.importorskip("psycopg")

    def forbidden_connect(*args, **kwargs):
        raise AssertionError("invalid settings reached the network")

    monkeypatch.setattr(psycopg, "connect", forbidden_connect)
    with pytest.raises(ValueError) as error:
        add_postgres_source("invalid", **(CONNECT | changed))
    assert SECRET not in str(error.value)


@pytest.mark.parametrize("stage", ["connect", "introspect"])
def test_connection_failures_hide_credentials_and_release_open_connection(stage, monkeypatch):
    psycopg = pytest.importorskip("psycopg")
    raw = SimpleNamespace(closed=False)

    def close():
        raw.closed = True

    raw.close = close

    def fail(*args, **kwargs):
        raise RuntimeError(f"postgresql://reader:{SECRET}@localhost/app")

    monkeypatch.setattr(psycopg, "connect", fail if stage == "connect" else lambda **kwargs: raw)
    monkeypatch.setattr(ibis.postgres, "from_connection", fail)
    with pytest.raises(PayloadError) as error:
        add_postgres_source("failed", **CONNECT)
    assert SECRET not in "".join(traceback.format_exception(error.value))
    assert "postgresql://" not in str(error.value)
    if stage == "introspect":
        assert raw.closed


def test_source_repr_omits_driver_and_connection_metadata():
    source = DataSource(
        id="pg", display="PostgreSQL", doc="", kind="postgres-live", dialect="postgres",
        dir=Path("."), con=SimpleNamespace(password=SECRET),
        postgres={"host": "localhost", "password": SECRET},
    )
    assert SECRET not in repr(source)


@pytest.mark.parametrize("name", ['collision"; DROP TABLE items; --', "x" * 64])
def test_unsafe_identifiers_never_reach_postgres(name):
    def forbidden_cursor():
        raise AssertionError("unsafe identifier reached PostgreSQL")

    con = SimpleNamespace(name="postgres", con=SimpleNamespace(cursor=forbidden_cursor))
    for action in (tx.drop_temp, tx.savepoint, tx.rollback_to, tx.release):
        with pytest.raises(PayloadError):
            action(con, name)
    with pytest.raises(PayloadError):
        tx.create_temp(con, name, None)


@pytest.fixture()
def postgres_database():
    if os.environ.get("D8R_TEST_POSTGRES") != "1":
        pytest.skip("set D8R_TEST_POSTGRES=1 to use an explicitly chosen test database")
    psycopg = pytest.importorskip("psycopg")
    sql = psycopg.sql
    options = dict(
        host=os.environ.get("PGHOST", "127.0.0.1"),
        port=int(os.environ.get("PGPORT", "5432")),
        database=os.environ.get("PGDATABASE", "postgres"),
        user=os.environ.get("PGUSER", "postgres"),
        password=os.environ.get("PGPASSWORD", "postgres"),
        sslmode=os.environ.get("PGSSLMODE", "prefer"),
    )
    # A quoted dot is one schema name, not a catalog/schema hierarchy or an
    # opportunity to alter search_path. Exercise that boundary on the server.
    schema = f'd8r_it_{uuid.uuid4().hex[:12]}."quoted'
    empty = schema + "_empty"
    other = schema + "_other"
    admin = psycopg.connect(
        host=options["host"], port=options["port"], dbname=options["database"],
        user=options["user"], password=options["password"], sslmode=options["sslmode"],
        autocommit=True, connect_timeout=10,
    )
    schemas = [schema, empty, other]
    try:
        for name in schemas:
            admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name))).close()
        for name in (schema, other):
            admin.execute(sql.SQL(
                "CREATE TABLE {}.items (id INTEGER PRIMARY KEY, amount NUMERIC, region TEXT)"
            ).format(sql.Identifier(name))).close()
        admin.execute(sql.SQL(
            "INSERT INTO {}.items VALUES (1, 10, 'emea'), (2, 30, 'amer'), (3, 20, 'emea')"
        ).format(sql.Identifier(schema))).close()
        admin.execute(sql.SQL("INSERT INTO {}.items VALUES (777, 1, 'other')").format(
            sql.Identifier(other)
        )).close()
        admin.execute(sql.SQL(
            "CREATE VIEW {}.selected_items AS SELECT id, region FROM {}.items WHERE id <= 2"
        ).format(sql.Identifier(schema), sql.Identifier(schema))).close()
        # Any attempt to COUNT this view during discovery raises division by
        # zero. Schema introspection and a correct no-count connection succeed.
        admin.execute(sql.SQL(
            "CREATE VIEW {}.unreadable_rows AS SELECT id FROM {}.items WHERE 1 / (id - id) > 0"
        ).format(sql.Identifier(schema), sql.Identifier(schema))).close()
        admin.execute(sql.SQL("CREATE TABLE {}.collision (id INTEGER)").format(
            sql.Identifier(schema)
        )).close()
        admin.execute(sql.SQL("INSERT INTO {}.collision VALUES (99)").format(
            sql.Identifier(schema)
        )).close()
        yield options, schema, empty, admin
    finally:
        for name in reversed(schemas):
            admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                sql.Identifier(name)
            )).close()
        admin.close()


@pytest.fixture()
def postgres_source(postgres_database):
    options, schema, _, _ = postgres_database
    source = add_postgres_source("pg-test", schema=schema, **options)
    try:
        yield source
    finally:
        source.con.disconnect()


def test_live_discovery_is_schema_scoped_and_never_counts(postgres_source, postgres_database):
    source = postgres_source
    assert set(source.datasets) == {"items", "selected_items", "unreadable_rows", "collision"}
    assert all(entry["rows"] is None for entry in source.datasets.values())
    result = execute(source.con, {
        "dataset": "items",
        "select": [{"column": "id"}],
        "where": {"column": "amount", "op": ">", "value": 15},
        "orderBy": [{"target": "id", "direction": "asc"}],
    }, dialect="postgres", tables={name: entry["table"] for name, entry in source.datasets.items()})
    assert result["rows"] == [[2], [3]]
    assert execute(source.con, {"dataset": "selected_items", "select": [{"column": "id"}],
                               "orderBy": [{"target": "id", "direction": "asc"}]})["rows"] == [[1], [2]]
    assert source.con.name == "postgres"
    assert source.con.con.autocommit
    assert source.postgres["schema"] == postgres_database[1]
    assert "password" not in source.postgres
    assert capabilities_for(source)["supports"]["savepoints"]
    caps = capabilities_for(source)
    # PostgreSQL's own compiler answers the probe: ILIKE and the operation
    # surface are native there.
    assert caps["supports"]["ilike"] is True
    assert all(
        caps["supports"][k]
        for k in ("quantile", "asofJoin", "sampling", "unnest")
    )
    # ibis 12 refuses a seeded sample on PostgreSQL (`Seeded samples are not
    # supported on this backend`), so the probe says False there even though
    # `TABLESAMPLE` itself is native. Unseeded sampling is True.
    assert caps["supports"]["samplingSeed"] is False
    offered = caps["functions"]["string"]
    assert "translate" in offered
    assert "as_timestamp" in offered and "as_date" in offered
    # levenshtein renders but only runs when the server provides the
    # function (fuzzystrmatch); the offer tracks the server's pg_proc.
    _, schema, _, admin = postgres_database
    with admin.cursor() as cursor:
        server_has = bool(cursor.execute(
            "select 1 from pg_proc where proname = 'levenshtein'"
        ).fetchall())
    assert ("levenshtein" in offered) is server_has
    # ibis 12 has no PostgreSQL rule for the URL accessors or convert_base.
    assert not any(fn.startswith("url_") for fn in offered)
    assert "convert_base" not in offered


def test_numeric_values_serialize_as_plain_decimal_text(postgres_source):
    """A NUMERIC 30 arrives through ibis's arrow path as Decimal('3E+1').

    `str()` of that Decimal is scientific notation — the results pane would
    show a corrupted money figure. Serialization must render every digit.
    """
    result = execute(postgres_source.con, {
        "dataset": "items",
        "select": [{"column": "amount"}],
        "where": {"column": "id", "op": "=", "value": 2},
    }, dialect="postgres", tables={
        name: entry["table"] for name, entry in postgres_source.datasets.items()
    })
    assert result["rows"] == [["30"]]
    assert all(not (isinstance(v, str) and "E" in v.upper()) for row in result["rows"] for v in row)


def test_empty_schema_connects_without_creating_extensions(postgres_database):
    options, _, empty, admin = postgres_database
    with admin.cursor() as cursor:
        before = cursor.execute("SELECT extname FROM pg_extension ORDER BY extname").fetchall()
    source = add_postgres_source("empty", schema=empty, **options)
    try:
        assert source.datasets == {}
        with admin.cursor() as cursor:
            assert cursor.execute("SELECT extname FROM pg_extension ORDER BY extname").fetchall() == before
    finally:
        source.con.disconnect()


def test_live_query_and_savepoints_preserve_outer_transaction(postgres_source):
    con = postgres_source.con
    items = postgres_source.datasets["items"]["table"]
    tx.begin(con)
    tx.create_temp(con, "kept", items.filter(items.id == 1))
    tx.savepoint(con, "Before")
    tx.create_temp(con, "later", items.filter(items.id == 2))
    assert tx.temp_handle(con, "later", items.schema()).id.execute().tolist() == [2]
    # A failing Ibis SELECT rolls back its nested savepoint, not the user's
    # transaction. Successful execution likewise must not commit that owner.
    with pytest.raises(Exception):
        postgres_source.datasets["unreadable_rows"]["table"].execute()
    assert tx.temp_handle(con, "kept", items.schema()).id.execute().tolist() == [1]
    tx.rollback_to(con, "Before")
    tx.release(con, "Before")
    assert "later" not in con.list_tables(database=(postgres_source.postgres["database"], con._session_temp_db))
    tx.rollback(con)
    assert "kept" not in con.list_tables(database=(postgres_source.postgres["database"], con._session_temp_db))
    tx.begin(con)
    tx.create_temp(con, "committed", items.filter(items.id == 3))
    tx.commit(con)
    assert tx.temp_handle(con, "committed", items.schema()).id.execute().tolist() == [3]


def test_temp_collision_handles_cannot_fall_back_to_persistent_table(postgres_source):
    con = postgres_source.con
    persistent = postgres_source.datasets["collision"]["table"]
    items = postgres_source.datasets["items"]["table"].select("id")
    tx.drop_temp(con, "collision")
    tx.create_temp(con, "collision", items.filter(items.id == 1))
    handle = tx.temp_handle(con, "collision", items.schema())
    assert handle.id.execute().tolist() == [1]
    assert persistent.id.execute().tolist() == [99]
    tx.begin(con)
    tx.create_temp(con, "collision", items.filter(items.id == 2))
    assert handle.id.execute().tolist() == [2]
    tx.rollback(con)
    assert handle.id.execute().tolist() == [1]
    tx.begin(con)
    tx.drop_temp(con, "collision")
    tx.rollback(con)
    assert handle.id.execute().tolist() == [1]
    tx.drop_temp(con, "collision")
    with pytest.raises(Exception):
        handle.execute()
    assert persistent.id.execute().tolist() == [99]


def test_aborted_transaction_cannot_report_a_successful_commit(postgres_source):
    con = postgres_source.con
    tx.begin(con)
    tx.create_temp(con, "uncommitted", postgres_source.datasets["items"]["table"])
    with pytest.raises(PayloadError):
        tx.release(con, "missing_savepoint")
    with pytest.raises(PayloadError, match="roll back"):
        tx.commit(con)
    tx.rollback(con)
    assert "uncommitted" not in con.list_tables(database=(
        postgres_source.postgres["database"], con._session_temp_db
    ))
