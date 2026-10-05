"""The "Add Data Source" surface for Cloudflare D1, in both shapes.

A *snapshot* source opens a local SQLite file through ibis's real SQLite
backend — in-process, credential-free, never a socket. A *live* source reaches
a hosted D1 over Cloudflare's HTTPS API; here that socket is faked with
`httpx.MockTransport` standing in for api.cloudflare.com, so the whole remote
path — resolve, introspect, compile-to-SQLite, run, map rows — is exercised
offline. The token is asserted to ride out in the request header and to never
come back in a response body.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing

import httpx
import pytest

from d8r.engine import (
    CloudflareD1,
    D1Error,
    add_d1_live_source,
    add_sqlite_source,
    capabilities_for,
    column_values,
    execute,
    execute_remote,
    expression,
)
from d8r.engine.make_data import write_d1_snapshot
from d8r.query import ForeignKey
from tests.conftest import DATA_DIR

SNAPSHOT_TABLES = {"orders", "customers"}


@pytest.fixture()
def sqlite_file(tmp_path):
    """A minimal stand-in for a D1 snapshot, including the internal objects
    D1's own introspection hides (`_cf_*`) and SQLite hides as reserved."""
    path = tmp_path / "snapshot.sqlite"
    con = sqlite3.connect(path)
    con.executescript(
        """
        create table customers(id integer primary key, region text);
        create table orders(id integer primary key, customer_id integer references customers(id), amount real);
        create table _cf_meta(x integer);
        insert into customers values (1, 'emea'), (2, 'amer');
        insert into orders values (1, 1, 10.0), (2, 1, 5.0), (3, 2, 7.5);
        """
    )
    con.commit()
    con.close()
    return str(path)


# ---------------------------------------------------------------------------
# Local D1 snapshots — a real SQLite file, opened on demand.
# ---------------------------------------------------------------------------


def test_snapshot_source_reports_its_datasets(sqlite_file):
    source = add_sqlite_source("d1", sqlite_file, "D1 prod")
    assert source.kind == "d1"
    assert source.dialect == "sqlite"
    assert source.display == "D1 prod"
    assert set(source.datasets) == SNAPSHOT_TABLES
    assert {name: e["rows"] for name, e in source.datasets.items()} == {"customers": 2, "orders": 3}
    assert source.datasets["customers"]["foreign_keys"] == ()
    assert source.datasets["orders"]["foreign_keys"] == (ForeignKey(("customer_id",), "customers", ("id",)),)


def test_snapshot_source_filters_internal_objects(sqlite_file):
    """`_cf_*`/`sqlite_*` plumbing stays out of the explorer, the same filter
    D1's own console applies."""
    source = add_sqlite_source("d1", sqlite_file)
    assert all(not name.startswith(("_cf_", "sqlite_")) for name in source.datasets)


def test_snapshot_source_executes_and_compiles(sqlite_file):
    """A payload against a snapshot runs on the real SQLite engine and still
    compiles to every advertised dialect (rows are backend-owned, SQL is not)."""
    source = add_sqlite_source("d1", sqlite_file)
    payload = {
        "dataset": "orders",
        "select": [{"column": "customer_id"}],
        "where": {"column": "amount", "op": ">", "value": 6.0},
        "orderBy": [{"target": "customer_id", "direction": "asc"}],
        "limit": 2,
    }
    executed = execute(source.con, payload, dialect=source.dialect)
    assert executed["rows"] == [[1], [2]]
    assert executed["sql"].strip().endswith("LIMIT 2")
    rendered = expression.compile_sql(expression.build(source.con, payload), dialect="postgres")
    assert "orders" in rendered


def test_snapshot_values_survive_a_thread_switch(sqlite_file):
    """Value lookups may run off the creating thread; the SQLite handle must
    not refuse (the default same-thread check would raise)."""
    source = add_sqlite_source("d1", sqlite_file)
    box: list[list[str]] = []
    worker = threading.Thread(target=lambda: box.append(column_values(source, "customers", "region")))
    worker.start()
    worker.join()
    assert box == [["amer", "emea"]]


def test_capabilities_honest_for_snapshot(sqlite_file):
    """A D1 snapshot is a real SQLite engine, not a mock — it says so, and its
    operation flags are probed against the SQLite backend, not copied from
    DuckDB."""
    caps = capabilities_for(add_sqlite_source("d1", sqlite_file))
    assert caps["backend"] == "sqlite (D1 snapshot)"
    supports = caps["supports"]
    assert supports["savepoints"] is True
    # Measured SQLite truth: no quantile rule, no as-of join, no seeded
    # sample, no native unnest — plain sampling and ILIKE (LOWER … LIKE) are
    # pure SQL and stay.
    assert supports["quantile"] is False
    assert supports["asofJoin"] is False
    assert supports["samplingSeed"] is False
    assert supports["unnest"] is False
    assert supports["sampling"] is True
    assert supports["ilike"] is True
    offered = caps["functions"]["string"]
    # The snapshot's SQLite backend registers UDFs the mock duckdb has no
    # rule for: translate and the URL accessors are offered here…
    assert "translate" in offered and "url_host" in offered
    # …while rules no SQLite backend has (levenshtein, strptime casts,
    # convert_base) are not.
    for fn in ("levenshtein", "as_date", "as_time", "as_timestamp", "convert_base"):
        assert fn not in offered


def test_snapshot_source_requires_an_explicit_path(tmp_path):
    with pytest.raises(ValueError, match="path is required"):
        add_sqlite_source("d1", "   ")
    with pytest.raises(FileNotFoundError):
        add_sqlite_source("d1", str(tmp_path / "missing.sqlite"))


def test_bundled_snapshot_opens():
    """The shipped snapshot contains only the known synthetic datasets and rows."""
    source = add_sqlite_source("d1", str(DATA_DIR.parent / "d1" / "d1.sqlite"))
    try:
        assert {name: entry["rows"] for name, entry in source.datasets.items()} == {
            "stations": 3,
            "readings": 12,
        }
        assert source.con.table("stations").columns == ("station_id", "label")
        assert source.con.table("readings").columns == (
            "reading_id", "station_id", "temperature_c",
        )
        assert source.datasets["readings"]["foreign_keys"] == (
            ForeignKey(("station_id",), "stations", ("station_id",)),
        )
        assert source.datasets["stations"]["foreign_keys"] == ()
        stations = execute(source.con, {
            "dataset": "stations",
            "orderBy": [{"target": "station_id", "direction": "asc"}],
        }, dialect=source.dialect)
        assert stations["rows"] == [[1, "Demo North"], [2, "Demo Central"], [3, "Demo South"]]
        readings = execute(source.con, {
            "dataset": "readings",
            "orderBy": [{"target": "reading_id", "direction": "asc"}],
        }, dialect=source.dialect)
        assert readings["rows"] == [
            [1, 1, 18.0], [2, 2, 18.5], [3, 3, 19.0],
            [4, 1, 19.5], [5, 2, 20.0], [6, 3, 20.5],
            [7, 1, 21.0], [8, 2, 21.5], [9, 3, 22.0],
            [10, 1, 22.5], [11, 2, 23.0], [12, 3, 23.5],
        ]
    finally:
        source.con.disconnect()


def test_snapshot_generation_replaces_old_pages_deterministically(tmp_path):
    """Deleted content in an old file must not survive synthetic regeneration."""
    old = tmp_path / "old.sqlite"
    marker = "discarded-fixture-only-marker-" * 1000
    with closing(sqlite3.connect(old)) as con:
        con.execute("PRAGMA secure_delete = OFF")
        con.execute("CREATE TABLE discarded (value TEXT)")
        con.execute("INSERT INTO discarded VALUES (?)", (marker,))
        con.commit()
        con.execute("DROP TABLE discarded")
        con.commit()
    assert marker.encode()[:100] in old.read_bytes()

    write_d1_snapshot(old)
    expected = write_d1_snapshot(tmp_path / "new" / "fresh.sqlite").read_bytes()
    assert old.read_bytes() == expected
    assert marker.encode()[:100] not in expected
    write_d1_snapshot(old)
    assert old.read_bytes() == expected


# ---------------------------------------------------------------------------
# Live Cloudflare D1 — the whole remote path, against a faked REST API.
# ---------------------------------------------------------------------------

# A token distinctive enough that an accidental echo in any response body
# fails the tests that scan for it.
SECRET = "super-secret-token-xyz"

SCHEMA_SQL = """
    create table customers (id integer not null, region text);
    create table orders (id integer not null, customer_id integer not null, amount real, paid text);
"""

LIVE_ARGS = {"account_id": "acct-1", "database": "orders-db", "api_token": SECRET}


def _envelope(result) -> dict:
    return {"success": True, "errors": [], "messages": [], "result": result}


def fake_cloudflare(exec_rows: dict | None = None, bad_token: bool = False):
    """Return `(client, sent)`: a live-D1 client to hand to `add_d1_live_source`,
    plus the list of SQL strings it received. Stands in for api.cloudflare.com."""
    sent: list[str] = []
    exec_rows = exec_rows or {"columns": ["id", "amount", "paid"], "rows": [[1, 10.0, "paid"], [2, 5.0, "new"]]}

    def handler(request: httpx.Request) -> httpx.Response:
        if bad_token:
            return httpx.Response(401, json={"success": False, "errors": [{"message": "Invalid API Token"}]})
        auth = request.headers.get("authorization", "")
        assert auth == f"Bearer {SECRET}", "the D1 token must ride in the Authorization header"
        if request.method == "GET":
            # resolve(name) → the database's uuid.
            return httpx.Response(200, json=_envelope([{"uuid": "11111111-2222-3333-4444-555555555555", "name": "orders-db"}]))
        sql = json.loads(request.content)["sql"]
        sent.append(sql)
        if request.url.path.endswith("/query"):
            if sql.strip().rstrip(";").upper() == "SELECT 1":
                rows = [{"1": 1}]
            else:
                assert "sqlite_schema" in sql
                # Execute both full and selective metadata queries: never return
                # every table merely because the request mentions sqlite_schema.
                with closing(sqlite3.connect(":memory:")) as database:
                    database.row_factory = sqlite3.Row
                    database.executescript(SCHEMA_SQL)
                    rows = [dict(row) for row in database.execute(sql).fetchall()]
            return httpx.Response(200, json=_envelope([{"success": True, "results": rows}]))
        assert request.url.path.endswith("/raw")
        # raw(): run the compiled query/where/values SQL.
        return httpx.Response(200, json=_envelope([{"success": True, "results": exec_rows}]))

    client = CloudflareD1(
        **LIVE_ARGS, _client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    return client, sent


def live_source(exec_rows: dict | None = None, bad_token: bool = False):
    """A registered-shape live D1 source built over the faked API, plus the SQL
    the fake received."""
    client, sent = fake_cloudflare(exec_rows=exec_rows, bad_token=bad_token)
    return add_d1_live_source("orders-db", client=client, **LIVE_ARGS), sent


def test_live_source_requires_all_credentials():
    with pytest.raises(D1Error, match="Account ID"):
        add_d1_live_source("orders-db", account_id="", database="orders-db", api_token=SECRET)


def test_live_source_registers_without_leaking_the_token():
    source, _ = live_source()
    assert source.kind == "d1-live" and source.dialect == "sqlite"
    # Nothing the UI may render carries the token.
    rendered = {
        "id": source.id,
        "display": source.display,
        "doc": source.doc,
        "kind": source.kind,
        "dialect": source.dialect,
        "datasets": [{"name": n, "doc": e["doc"], "rows": e["rows"]} for n, e in source.datasets.items()],
        "capabilities": capabilities_for(source),
    }
    assert SECRET not in json.dumps(rendered)


@pytest.mark.parametrize("declared,notnull,expected", [
    # DATETIME must not lose to the DATE prefix; TIMESTAMP must not lose to it
    # either — the prefix rules run longest-key-first.
    ("DATETIME", 0, "timestamp"),
    ("DATETIME", 1, "!timestamp"),
    ("TIMESTAMP", 0, "timestamp"),
    ("DATE", 0, "date"),
    ("VARCHAR(50)", 0, "string"),
    ("CHARACTER", 0, "string"),
    ("NUMERIC(10,2)", 0, "float64"),
    ("BOOLEAN", 0, "boolean"),
    ("INTEGER", 1, "!int64"),
    ("WEIRD_TYPE", 0, "string"),
])
def test_declared_types_map_longest_prefix_first(declared, notnull, expected):
    from d8r.engine.d1api import _ibis_type

    assert _ibis_type(declared, notnull) == expected


def test_live_capabilities_refuse_udf_backed_functions_and_regex():
    """Cloudflare D1 has no `_IBIS_*` Python UDFs, so the capabilities the
    palette reads must not offer what cannot run there — and must not
    over-refuse either: `capitalize` compiles to pure SQL and stays."""
    source, _ = live_source()
    caps = capabilities_for(source)
    assert caps["backend"] == "sqlite (Cloudflare D1)"
    assert "~" not in caps["operators"] and "!~" not in caps["operators"]
    assert caps["supports"]["regex"] is False
    offered = caps["functions"]["string"]
    for fn in (
        "reverse", "repeat", "lpad", "rpad", "regexp_extract", "regexp_replace",
        "translate", "levenshtein", "as_date", "as_time", "as_timestamp",
    ):
        assert fn not in offered
    for fn in ("capitalize", "lower", "upper", "replace", "find", "substr"):
        assert fn in offered
    # `ilike` is pure SQL (`LOWER … LIKE`) — D1 runs it; the probed operation
    # flags are the honest static SQLite drop (no quantile/as-of/seed/unnest).
    assert "ilike" in caps["operators"]
    assert caps["supports"]["ilike"] is True
    assert caps["supports"]["quantile"] is False
    assert caps["supports"]["asofJoin"] is False
    assert caps["supports"]["samplingSeed"] is False
    assert caps["supports"]["unnest"] is False


def test_live_connection_defers_schema_discovery():
    source, sent = live_source()
    try:
        assert source.datasets == {}
        assert source.con.list_tables() == []
        assert not source.schema_indexed
        assert len(sent) == 1
        assert sent[0].strip().rstrip(";").upper() == "SELECT 1"
    finally:
        source.d1.close()


def test_live_execute_runs_sqlite_sql_on_the_api():
    source, sent = live_source()
    payload = {
        "dataset": "orders",
        "select": [{"column": "id"}, {"column": "amount"}],
        "where": None, "joins": [], "group": None, "order": None, "limit": 5, "case": None, "with": [],
    }
    result = execute_remote(source.d1, source.con, payload, "postgres")
    assert result["rows"] == [[1, 10.0, "paid"], [2, 5.0, "new"]]
    assert result["dialect"] == "postgres"
    # The SQL actually POSTed to D1 is SQLite (LIMIT, quoted table), not postgres.
    ran = [s for s in sent if "LIMIT" in s.upper()]
    assert ran and all('"orders"' in s for s in ran)
    # The *displayed* SQL is the requested dialect.
    assert result["sql"].strip().upper().startswith("SELECT")


def test_indexed_live_column_values_map_through_the_api():
    source, sent = live_source(exec_rows={"columns": ["region"], "rows": [["amer"], ["emea"]]})
    schemas = source.d1.schemas()
    source.con.seed(schemas)
    source.datasets = {
        name: {"table": source.con.table(name), "doc": name, "rows": None}
        for name in schemas
    }
    source.schema_indexed = True
    assert column_values(source, "customers", "region") == ["amer", "emea"]
    assert any("DISTINCT" in s.upper() and "region" in s for s in sent)


def test_live_bad_token_surfaces_message():
    client, _ = fake_cloudflare(bad_token=True)
    with pytest.raises(D1Error, match="Invalid API Token"):
        add_d1_live_source("orders-db", client=client, **LIVE_ARGS)


def test_live_compile_from_indexed_schema_renders_without_executing():
    source, sent = live_source()
    source.con.seed(source.d1.schemas())
    sent.clear()
    payload = {"dataset": "orders", "select": [{"column": "id"}], "where": None, "joins": [], "group": None, "order": None, "limit": 3, "case": None, "with": []}
    compiled = expression.compile_sql(expression.build(source.con, payload), dialect="postgres")
    assert '"orders"' in compiled
    assert sent == []  # compiling never talks to D1


def test_live_join_and_aggregate_build_on_unbound_schema():
    r"""Real queries run on the user's live D1: a `\join` + aggregate payload
    must build against the unbound schema tables and compile to SQLite SQL the
    API runs — proving the feature is not single-table-only."""
    source, sent = live_source(exec_rows={"columns": ["region", "amount_sum"], "rows": [["amer", 15.0], ["emea", 7.5]]})
    sent.clear()
    payload = {
        "dataset": "orders",
        "joins": [{"dataset": "customers", "left": "customer_id", "right": "id"}],
        "select": [
            {"column": "region"},
            {"aggregate": {"fn": "sum", "arg": "amount"}, "alias": "amount_sum"},
        ],
        "groupBy": ["region"],
    }
    result = execute_remote(source.d1, source.con, payload, source.dialect)
    assert result["rows"] == [["amer", 15.0], ["emea", 7.5]]
    joined = [s for s in sent if "JOIN" in s.upper() and "sqlite_schema" not in s]
    assert joined, "the compiled SQLite SQL must carry the join"
    assert all('"customers"' in s and '"orders"' in s for s in joined)


@pytest.fixture()
def sqlite_cloudflare():
    """Execute the API's discovery SQL, rather than answering a substring match."""
    database = sqlite3.connect(":memory:")
    database.row_factory = sqlite3.Row
    database.setlimit(sqlite3.SQLITE_LIMIT_COMPOUND_SELECT, 5)
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sql = json.loads(request.content)["sql"]
        sent.append(sql)
        try:
            rows = [dict(row) for row in database.execute(sql).fetchall()]
        except sqlite3.Error as exc:
            return httpx.Response(200, json=_envelope([
                {"success": False, "errors": [{"message": str(exc)}]},
            ]))
        return httpx.Response(200, json=_envelope([{"success": True, "results": rows}]))

    client = CloudflareD1(
        account_id=LIVE_ARGS["account_id"], api_token=SECRET,
        database="11111111-2222-3333-4444-555555555555",
        _client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    try:
        yield database, client, sent
    finally:
        client.close()
        database.close()


def test_live_discovery_executes_valid_sqlite_sql(sqlite_cloudflare):
    database, client, _ = sqlite_cloudflare
    database.executescript('''
        create table "order details" ("notnull" integer not null, "quoted""name" text);
        insert into "order details" values (1, 'first'), (2, null);
        create table _cf_meta (internal text);
        create view "public view" as select * from "order details";
    ''')
    source = add_d1_live_source("live", client=client, **LIVE_ARGS)
    schemas = client.schemas()
    assert set(schemas) == {"order details", "public view"}
    assert source.con.list_tables() == []
    source.con.seed(schemas)
    assert set(source.con.list_tables()) == {"order details", "public view"}
    schema = source.con.table("order details").schema()
    assert schema.names == ("notnull", 'quoted"name')
    assert not schema["notnull"].nullable
    assert schema['quoted"name'].nullable


def test_live_connection_does_not_read_table_or_view_data(sqlite_cloudflare):
    database, client, _ = sqlite_cloudflare
    database.executescript('''
        create table records (id integer);
        create view report as select * from records;
    ''')

    def authorize(action, table, column, schema, trigger):
        if action == sqlite3.SQLITE_READ and table in {"records", "report"}:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    database.set_authorizer(authorize)
    source = add_d1_live_source("metadata-only", client=client, **LIVE_ARGS)
    assert source.datasets == {}
    assert source.con.list_tables() == []
    assert not source.schema_indexed


def test_live_connection_and_query_survive_unrelated_discovery_failure(sqlite_cloudflare):
    database, client, _ = sqlite_cloudflare
    database.executescript('''
        create table records (id integer);
        create view broken as select * from missing;
    ''')
    source = add_d1_live_source("partially-indexable", client=client, **LIVE_ARGS)
    expr = expression.build(source.con, {"dataset": "records"})
    assert expr.schema().names == ("id",)
    with pytest.raises(D1Error, match="missing"):
        client.schemas()
    # A failed background discovery neither closes the client nor invalidates
    # the metadata already fetched for a queryable table.
    assert source.con.table("records").schema().names == ("id",)
    assert not client._client.is_closed


def test_lazy_metadata_is_selective_quoted_and_cached(sqlite_cloudflare):
    database, client, sent = sqlite_cloudflare
    database.executescript('''
        create table "order's ""details" (id integer primary key autoincrement, amount real);
        create table other (name text);
        create table _cf_meta (internal text);
        create view "public view" as select name from other;
        create view broken as select * from missing;
    ''')
    source = add_d1_live_source("selective", client=client, **LIVE_ARGS)
    name = 'order\'s "details'
    sent.clear()
    assert source.con.table(name).schema().names == ("id", "amount")
    assert all("sqlite_schema" in sql and "m.name in" in sql for sql in sent)
    assert source.con.list_tables() == [name]
    sent.clear()
    assert source.con.table(name).schema().names == ("id", "amount")
    assert sent == []

    # A literal containing an SQL injection attempt must remain one name, not
    # broaden discovery to the unrelated (and deliberately broken) view.
    schemas = client.schemas([name, "public view", "_cf_meta", "sqlite_sequence", "x') OR 1=1 --"])
    assert set(schemas) == {name, "public view"}
    assert source.con.list_tables() == [name]
    source.con.seed(schemas)
    assert set(source.con.list_tables()) == {name, "public view"}
    sent.clear()
    assert client.schemas([]) == {}
    assert source.con.table("public view").schema().names == ("name",)
    assert sent == []
    for hidden in ("_cf_meta", "sqlite_sequence", "absent"):
        with pytest.raises(KeyError):
            source.con.table(hidden)


@pytest.mark.parametrize("live", [False, True], ids=["snapshot", "live"])
def test_foreign_keys_preserve_pairs_and_resolve_implicit_primary_keys(sqlite_cloudflare, tmp_path, live):
    database, client, sent = sqlite_cloudflare
    database.executescript('''
        create table "parent's key" (
            second integer, first integer, primary key(first, second), unique(second, first)
        );
        create table child (
            first_ref integer, second_ref integer,
            foreign key(first_ref, second_ref) references "parent's key",
            foreign key(second_ref, first_ref) references "parent's key"(second, first)
        );
        create table unresolved (ref integer references missing);
        create table mismatched (ref integer references "parent's key");
    ''')
    expected = (
        ForeignKey(("second_ref", "first_ref"), "parent's key", ("second", "first")),
        ForeignKey(("first_ref", "second_ref"), "parent's key", ("first", "second")),
    )
    if live:
        # Introspection remains possible even when all user-row reads are denied.
        def authorize(action, table, column, schema, trigger):
            if action == sqlite3.SQLITE_READ and table in {"child", "parent's key", "unresolved", "mismatched"}:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        database.set_authorizer(authorize)
        source = add_d1_live_source("fk-live", client=client, **LIVE_ARGS)
        source.con.table("child")
        assert source.con.list_tables() == ["child"]
        sent.clear()
        assert source.con.foreign_keys("child") == expected
        assert source.con.foreign_keys("unseen") == ()
        assert sent == []
        schemas = client.schemas()
        source.con.seed(schemas)
        keys = {name: source.con.foreign_keys(name) for name in source.con.list_tables()}
    else:
        path = tmp_path / "foreign-keys.sqlite"
        with closing(sqlite3.connect(path)) as snapshot:
            database.backup(snapshot)
        source = add_sqlite_source("fk-snapshot", str(path))
        keys = {name: entry["foreign_keys"] for name, entry in source.datasets.items()}
    try:
        assert keys["child"] == expected
        assert keys["parent's key"] == ()
        assert keys["unresolved"] == (ForeignKey(("ref",), "missing", ()),)
        assert keys["mismatched"] == (ForeignKey(("ref",), "parent's key", ()),)
    finally:
        if not live:
            source.con.disconnect()


def test_foreign_key_discovery_failure_does_not_publish_partial_metadata(sqlite_cloudflare, monkeypatch):
    database, client, _ = sqlite_cloudflare
    database.executescript('''
        create table parent (id integer primary key);
        create table child (ref integer references parent);
        create table unseen (ref integer references parent);
    ''')
    source = add_d1_live_source("fk-failure", client=client, **LIVE_ARGS)
    source.con.table("child")
    known = source.con.foreign_keys("child")
    query = client.query

    def fail_constraints(sql):
        if "pragma_foreign_key_list" in sql:
            raise D1Error("constraint metadata unavailable")
        return query(sql)

    monkeypatch.setattr(client, "query", fail_constraints)
    with pytest.raises(D1Error, match="constraint metadata unavailable"):
        source.con.table("unseen")
    with pytest.raises(D1Error, match="constraint metadata unavailable"):
        client.schemas()
    assert source.con.list_tables() == ["child"]
    assert source.con.foreign_keys("child") == known == (ForeignKey(("ref",), "parent", ("id",)),)
    assert not source.schema_indexed


@pytest.mark.parametrize("failure", ["auth", "transport"])
@pytest.mark.parametrize("lookup", ["root", "join"])
def test_lazy_lookup_failures_propagate_through_build(failure, lookup):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/query")
        sql = json.loads(request.content)["sql"]
        if sql.strip().rstrip(";").upper() == "SELECT 1":
            return httpx.Response(200, json=_envelope([{"success": True, "results": [{"1": 1}]}]))
        if failure == "transport":
            raise httpx.ConnectError("network unavailable", request=request)
        return httpx.Response(401, json={"success": False, "errors": [{"message": "Invalid API Token"}]})

    client = CloudflareD1(
        account_id=LIVE_ARGS["account_id"], api_token=SECRET,
        database="11111111-2222-3333-4444-555555555555",
        _client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    try:
        source = add_d1_live_source("credentials-expired", client=client, **LIVE_ARGS)
        payload = {"dataset": "customers"}
        if lookup == "join":
            # An indexed left table must not hide a failed lookup of its join.
            source.con.seed({"orders": {"customer_id": "int64"}})
            payload = {
                "dataset": "orders",
                "joins": [{"dataset": "customers", "left": "customer_id", "right": "id"}],
            }
        message = "Invalid API Token" if failure == "auth" else "Could not reach Cloudflare"
        with pytest.raises(D1Error, match=message):
            expression.build(source.con, payload, tables={})
    finally:
        client.close()


def test_empty_live_database_connects_without_count_queries(sqlite_cloudflare):
    database, client, sent = sqlite_cloudflare
    database.execute("create table _cf_meta (internal text)")
    source = add_d1_live_source("empty", client=client, **LIVE_ARGS)
    assert source.kind == "d1-live"
    assert source.datasets == {}
    assert source.con.list_tables() == []
    assert not any("count(*)" in sql for sql in sent)
    assert not client._client.is_closed


def test_database_name_is_encoded_and_unknown_names_never_execute_sql():
    name = "production & region=other/#?"
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.url.params["name"] == name
        assert "region" not in request.url.params
        assert not request.url.fragment
        return httpx.Response(200, json=_envelope([]))

    client = CloudflareD1(
        account_id="acct-1", database=name, api_token=SECRET,
        _client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(D1Error, match="No D1 database matched"):
        add_d1_live_source("missing", "acct-1", SECRET, name, client=client)
    assert len(requests) == 1
    assert not client.database_uuid
    assert client._client.is_closed


def test_name_resolution_searches_later_pages():
    uuid = "11111111-2222-3333-4444-555555555555"
    pages = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        pages.append(page)
        rows = ([{"name": f"orders-db-{n}", "uuid": uuid} for n in range(1000)]
                if page == 1 else [{"name": "orders-db", "uuid": uuid}])
        return httpx.Response(200, json=_envelope(rows))

    client = CloudflareD1(**LIVE_ARGS, _client=httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        assert client.resolve() == uuid
        assert pages == [1, 2]
    finally:
        client.close()


@pytest.mark.parametrize("failure", ["api", "query", "transport"])
def test_token_is_redacted_from_client_repr_and_failures(failure):
    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "transport":
            raise httpx.ConnectError(f"unexpected credential {SECRET}", request=request)
        error = {"success": False, "errors": [{"message": f"Invalid API Token: {SECRET}"}]}
        if failure == "query":
            return httpx.Response(200, json=_envelope([error]))
        return httpx.Response(401, json=error)

    client = CloudflareD1(
        account_id="acct-1", database="11111111-2222-3333-4444-555555555555", api_token=SECRET,
        _client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert SECRET not in repr(client)
    try:
        with pytest.raises(D1Error) as error:
            client.schemas()
        assert SECRET not in str(error.value)
        assert SECRET not in repr(error.value)
    finally:
        client.close()


def test_failed_connection_probe_releases_client_even_for_non_d1_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("transport failed before responding")

    client = CloudflareD1(
        account_id="acct-1", database="11111111-2222-3333-4444-555555555555", api_token=SECRET,
        _client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(RuntimeError, match="transport failed"):
        add_d1_live_source("failed", client=client, **LIVE_ARGS)
    assert client._client.is_closed
