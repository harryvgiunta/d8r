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
        create table orders(id integer primary key, customer_id integer, amount real);
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
    """A D1 snapshot is a real SQLite engine, not a mock — it says so."""
    assert capabilities_for(add_sqlite_source("d1", sqlite_file))["backend"] == "sqlite (D1 snapshot)"


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

# orders(id, customer_id, amount REAL, paid TEXT); customers(id, region).
SCHEMA_ROWS = [
    {"source_table": "customers", "cid": 0, "name": "id", "type": "INTEGER", "notnull": 1},
    {"source_table": "customers", "cid": 1, "name": "region", "type": "TEXT", "notnull": 0},
    {"source_table": "orders", "cid": 0, "name": "id", "type": "INTEGER", "notnull": 1},
    {"source_table": "orders", "cid": 1, "name": "customer_id", "type": "INTEGER", "notnull": 1},
    {"source_table": "orders", "cid": 2, "name": "amount", "type": "REAL", "notnull": 0},
    {"source_table": "orders", "cid": 3, "name": "paid", "type": "TEXT", "notnull": 0},
]

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
        if "sqlite_schema" in sql:  # schemas(): correlated pragma
            return httpx.Response(200, json=_envelope([{"success": True, "results": SCHEMA_ROWS}]))
        if "count(*)" in sql:  # row_counts(): UNION ALL
            return httpx.Response(200, json=_envelope([{"success": True, "results": [{"dataset": "customers", "n": 2}, {"dataset": "orders", "n": 3}]}]))
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
    assert {name: e["rows"] for name, e in source.datasets.items()} == {"customers": 2, "orders": 3}
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
    for fn in ("reverse", "repeat", "lpad", "rpad", "regexp_extract", "regexp_replace"):
        assert fn not in offered
    for fn in ("capitalize", "lower", "upper", "replace", "find", "substr"):
        assert fn in offered


def test_live_probe_counts_tables():
    source, _ = live_source()
    assert source.kind == "d1-live"
    assert len(source.datasets) == 2
    source.d1.close()  # a probe's connection is released, never kept


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


def test_live_column_values_map_through_the_api():
    source, sent = live_source(exec_rows={"columns": ["region"], "rows": [["amer"], ["emea"]]})
    assert column_values(source, "customers", "region") == ["amer", "emea"]
    assert any("DISTINCT" in s.upper() and "region" in s for s in sent)


def test_live_bad_token_surfaces_message():
    client, _ = fake_cloudflare(bad_token=True)
    with pytest.raises(D1Error, match="Invalid API Token"):
        add_d1_live_source("orders-db", client=client, **LIVE_ARGS)


def test_live_compile_renders_without_executing():
    source, sent = live_source()
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
    joined = [s for s in sent if "JOIN" in s.upper()]
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
        rows = [dict(row) for row in database.execute(sql).fetchall()]
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
    assert set(source.datasets) == {"order details", "public view"}
    schema = source.con.table("order details").schema()
    assert schema.names == ("notnull", 'quoted"name')
    assert not schema["notnull"].nullable
    assert schema['quoted"name'].nullable
    assert {name: table["rows"] for name, table in source.datasets.items()} == {
        "order details": 2, "public view": 2,
    }


def test_live_discovery_counts_all_tables_across_compound_limit(sqlite_cloudflare):
    database, client, _ = sqlite_cloudflare
    expected = {}
    for index in range(12):
        name = f'''table {index:02d} ' "'''
        quoted = '"' + name.replace('"', '""') + '"'
        database.execute(f"create table {quoted} (id integer)")
        database.executemany(f"insert into {quoted} values (?)", [(n,) for n in range(index)])
        expected[name] = index
    database.execute('create view "last view" as select 1 as id')
    expected["last view"] = 1

    source = add_d1_live_source("many-tables", client=client, **LIVE_ARGS)

    assert {name: entry["rows"] for name, entry in source.datasets.items()} == expected
    assert set(source.con.list_tables()) == set(expected)


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


def test_failed_discovery_releases_client_even_for_non_d1_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("transport failed before responding")

    client = CloudflareD1(
        account_id="acct-1", database="11111111-2222-3333-4444-555555555555", api_token=SECRET,
        _client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(RuntimeError, match="transport failed"):
        add_d1_live_source("failed", client=client, **LIVE_ARGS)
    assert client._client.is_closed
