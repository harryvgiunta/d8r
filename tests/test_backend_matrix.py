"""Cross-backend row parity for the gap-report language surface.

The same synthetic `events`/`users` tables and the same documents run on every
available backend — the Parquet demo on DuckDB, a local SQLite snapshot, a live
Cloudflare D1 (its HTTP API faked with a real stock-SQLite engine on the other
end, so the D1 column stays offline and runs real SQL), and (live-gated) a real
PostgreSQL server — and must return identical rows. Set D8R_TEST_POSTGRES=1
with PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD to add the PostgreSQL column;
the default offline suite never connects to anything.

Amounts are IEEE doubles everywhere so no backend injects a Decimal; the
PostgreSQL NUMERIC serialization is pinned in `test_postgres.py`. Temporal
string parsing (as_date family) has backend-specific lowering and is covered
in `test_strings.py`.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from pathlib import Path

import ibis
import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from d8r.engine import (
    CloudflareD1,
    D1Error,
    add_d1_live_source,
    add_postgres_source,
    add_sqlite_source,
    capabilities_for,
    execute,
    execute_remote,
)
from d8r.engine import datasources
from d8r.engine.expression import build, compile_sql
from d8r.query import parse_query, payload_from_ast

# id, kind, amount, user_id, url, day, ts — the row NULLs are the point.
EVENTS = [
    (1, "click", 10.5, 1, "https://acme.test/docs/intro?q=1#sec", "2024-02-03", "2024-02-03 13:04:05"),
    (2, "view", 30.0, None, "https://other.test/a", "2024-02-04", None),
    (3, "CLICK", 40.0, 4, "not a url", "2024-02-05", "2024-02-05 00:00:00"),
    (4, "purchase", None, 4, None, "2024-02-06", "2024-02-06 08:00:00"),
]
USERS = [(1, "us"), (2, "eu"), (3, "emea"), (4, "us")]

EVENTS_SQL = ("CREATE TABLE events (id INTEGER PRIMARY KEY, kind TEXT, amount REAL,"
              " user_id INTEGER, url TEXT, day TEXT, ts TEXT)")
EVENTS_PG = ("CREATE TABLE {q}.events (id INTEGER PRIMARY KEY, kind TEXT,"
             " amount DOUBLE PRECISION, user_id INTEGER, url TEXT, day TEXT, ts TEXT)")
USERS_SQL = "CREATE TABLE users (id INTEGER PRIMARY KEY, region TEXT)"


def _write_parquet(root: Path) -> None:
    names = ["id", "kind", "amount", "user_id", "url", "day", "ts"]
    types = ["int64", "string", "float64", "int64", "string", "string", "string"]
    columns = list(zip(*EVENTS))
    table = pa.Table.from_arrays(
        [pa.array(column, type=pa.type_for_alias(kind)) for column, kind in zip(columns, types)],
        names=names,
    )
    pq.write_table(table, root / "events.parquet")
    user_columns = list(zip(*USERS))
    users = pa.Table.from_arrays(
        [pa.array(user_columns[0], type=pa.int64()), pa.array(user_columns[1], type=pa.string())],
        names=["id", "region"],
    )
    pq.write_table(users, root / "users.parquet")


@pytest.fixture(scope="module")
def duckdb_source(tmp_path_factory):
    root = tmp_path_factory.mktemp("matrix-parquet")
    _write_parquet(root)
    return datasources.load(root, demo_catalog={"events": "e", "users": "u"})["demo"]


@pytest.fixture(scope="module")
def sqlite_source(tmp_path_factory):
    path = tmp_path_factory.mktemp("matrix-sqlite") / "matrix.sqlite"
    raw = sqlite3.connect(str(path))
    raw.executescript(f"{EVENTS_SQL};{USERS_SQL};")
    raw.executemany("INSERT INTO events VALUES (?,?,?,?,?,?,?)", EVENTS)
    raw.executemany("INSERT INTO users VALUES (?,?)", USERS)
    raw.commit()
    raw.close()
    return add_sqlite_source("matrix-sqlite", str(path))


@pytest.fixture(scope="module")
def postgres_source():
    """The live column, built only when the environment names a test server."""
    if os.environ.get("D8R_TEST_POSTGRES") != "1":
        yield None
        return
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql as psql

    options = dict(
        host=os.environ.get("PGHOST", "127.0.0.1"),
        port=int(os.environ.get("PGPORT", "5432")),
        database=os.environ.get("PGDATABASE", "postgres"),
        user=os.environ.get("PGUSER", "postgres"),
        password=os.environ.get("PGPASSWORD", "postgres"),
        sslmode=os.environ.get("PGSSLMODE", "prefer"),
    )
    admin = psycopg.connect(
        host=options["host"], port=options["port"], dbname=options["database"],
        user=options["user"], password=options["password"], autocommit=True, connect_timeout=10,
    )
    schema = f"d8r_matrix_{uuid.uuid4().hex[:12]}"
    quoted = '"' + schema.replace('"', '""') + '"'
    try:
        admin.execute(psql.SQL("CREATE SCHEMA {}").format(psql.Identifier(schema)))
        admin.execute(EVENTS_PG.format(q=quoted))
        admin.execute(f"CREATE TABLE {quoted}.users (id INTEGER PRIMARY KEY, region TEXT)")
        with admin.cursor() as cursor:
            cursor.executemany(f"INSERT INTO {quoted}.events VALUES (%s,%s,%s,%s,%s,%s,%s)", EVENTS)
            cursor.executemany(f"INSERT INTO {quoted}.users VALUES (%s,%s)", USERS)
        source = add_postgres_source("matrix-pg", schema=schema, **options)
        try:
            yield source
        finally:
            source.con.disconnect()
    finally:
        admin.execute(psql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(psql.Identifier(schema)))
        admin.close()


@pytest.fixture(scope="module")
def d1_live_source():
    """The live-D1 column: the Cloudflare API faked with a real stock-SQLite
    engine on the other end. The transport is `httpx.MockTransport` (no
    network), but the SQL the app POSTs — always SQLite — runs on a genuine
    engine that registers no `_IBIS_*` UDFs, exactly like Cloudflare's. So the
    parity rows are real, and the §4.3 refusals fail the way D1 fails."""
    args = {
        "account_id": "acct-1",
        "api_token": "matrix-token",
        "database": "11111111-2222-3333-4444-555555555555",
    }
    engine = sqlite3.connect(":memory:")
    engine.executescript(f"{EVENTS_SQL};{USERS_SQL};")
    engine.executemany("INSERT INTO events VALUES (?,?,?,?,?,?,?)", EVENTS)
    engine.executemany("INSERT INTO users VALUES (?,?)", USERS)
    engine.commit()

    def envelope(result) -> dict:
        return {"success": True, "errors": [], "messages": [], "result": result}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":  # resolve(): name → uuid
            return httpx.Response(200, json=envelope([{"uuid": args["database"], "name": "matrix"}]))
        sql = json.loads(request.content)["sql"]
        if request.url.path.endswith("/query"):  # schemas() / row_counts()
            cursor = engine.execute(sql)
            names = [d[0] for d in cursor.description]
            rows = [dict(zip(names, row)) for row in cursor.fetchall()]
            return httpx.Response(200, json=envelope([{"success": True, "results": rows}]))
        try:  # raw(): the compiled SQLite SQL, on an engine with no UDFs
            cursor = engine.execute(sql)
            names = [d[0] for d in cursor.description]
            result = {"columns": names, "rows": [list(row) for row in cursor.fetchall()]}
        except sqlite3.Error as exc:  # Cloudflare answers SQL errors in the envelope
            return httpx.Response(
                200, json=envelope([{"success": False, "errors": [{"message": str(exc)}]}])
            )
        return httpx.Response(200, json=envelope([{"success": True, "results": result}]))

    client = CloudflareD1(**args, _client=httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        yield add_d1_live_source("matrix-d1", client=client, **args)
    finally:
        client.close()
        engine.close()

@pytest.fixture(scope="module")
def backends(duckdb_source, sqlite_source, d1_live_source, postgres_source):
    found = [("duckdb", duckdb_source), ("sqlite", sqlite_source), ("d1-live", d1_live_source)]
    if postgres_source is not None:
        found.append(("postgres", postgres_source))
    return found


def tables_of(source: datasources.DataSource) -> dict:
    return {name: entry["table"] for name, entry in source.datasets.items()}


def numeric(value):
    """Type-tolerant comparison normalization: duckdb widens nullable ints."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def rows(doc: str, source) -> list:
    """Run a document the way `Session.run` does: the same build path, and a
    source with a live D1 client runs its SQLite SQL over the API."""
    ast = parse_query(doc)
    assert not ast.errors, ast.errors
    payload = payload_from_ast(ast)
    if source.d1 is not None:
        result = execute_remote(source.d1, source.con, payload, "sqlite", tables=tables_of(source))
    else:
        result = execute(source.con, payload, None, tables=tables_of(source))
    return [[numeric(value) for value in row] for row in result["rows"]]


# (name, document, expected rows) — verified identical on every backend.
PARITY: list[tuple[str, str, list]] = [
    # `like` is SQL LIKE: `%` matches, and it does not contain.
    ("like-prefix", "\\from events\n\\select id\n\\where kind like 'c%'\n\\order id", [[1]]),
    ("like-underscore", "\\from events\n\\select id\n\\where kind like '_lick'\n\\order id", [[1]]),
    ("ilike", "\\from events\n\\select id\n\\where kind ilike 'CLICK'\n\\order id", [[1], [3]]),
    ("between-inclusive",
     "\\from events\n\\select id\n\\where amount between 10.5 and 40.0\n\\order id", [[1], [2], [3]]),
    ("is-null", "\\from events\n\\select id\n\\where amount is null\n\\order id", [[4]]),
    ("is-not-null", "\\from events\n\\select id\n\\where kind is not null\n\\order id", [[1], [2], [3], [4]]),
    ("in-list", "\\from events\n\\select id\n\\where kind in ('click', 'view')\n\\order id", [[1], [2]]),
    ("not-in-list",
     "\\from events\n\\select id\n\\where kind not in ('click', 'view')\n\\order id", [[3], [4]]),
    ("or", "\\from events\n\\select id\n\\where kind = 'CLICK' or amount = 10.5\n\\order id", [[1], [3]]),
    # `and` binds tighter than `or`: only the (click AND amount>20) half is empty.
    ("and-or-precedence",
     "\\from events\n\\select id\n\\where kind = 'CLICK' or kind = 'click' and amount > 20\n\\order id", [[3]]),
    # `( ... )` groups bind tighter than the flat precedence above: grouped,
    # the `or` filters first, so only the (view AND amount>20) row survives.
    ("group-precedence",
     "\\from events\n\\select id\n\\where (kind = 'click' or kind = 'view') and amount > 20\n\\order id",
     [[2]]),
    ("group-over-null",
     "\\from events\n\\select id\n\\where (kind = 'CLICK' or amount = 10.5) and user_id is not null\n\\order id",
     [[1], [3]]),
    ("group-inside-group",
     "\\from events\n\\select id\n\\where (kind = 'click' or (user_id = 4 and amount is null))\n\\order id",
     [[1], [4]]),
    ("group-in-ands",
     "\\from events\n\\select id\n\\where kind in ('click', 'view') and (amount > 20 or user_id is null)\n\\order id",
     [[2]]),
    ("regex", "\\from events\n\\select id\n\\where kind ~ 'c.*k'\n\\order id", [[1]]),
    ("regex-negated", "\\from events\n\\select id\n\\where kind !~ 'c.*k'\n\\order id", [[2], [3], [4]]),
    # A `\case` tree whose branches use the new predicate forms.
    ("case-tree",
     "\\from events\n\\select id\n\\case band = when amount > 30 then 'hi'"
     " when kind ilike 'vi%' then 'mid' else 'lo'\n\\order id",
     [[1, "lo"], [2, "mid"], [3, "hi"], [4, "lo"]]),
    ("coalesce", "\\from events\n\\select id\n\\select coalesce(user_id, 0)\n\\order id",
     [[1, 1], [2, 0], [3, 4], [4, 4]]),
    ("nullif", "\\from events\n\\select id\n\\select nullif(kind, 'click')\n\\order id",
     [[1, None], [2, "view"], [3, "CLICK"], [4, "purchase"]]),
    ("translate", "\\from events\n\\select id\n\\select translate(kind, 'c', 'C')\n\\order id",
     [[1, "CliCk"], [2, "view"], [3, "CLICK"], [4, "purChase"]]),
    ("join-group-sum",
     "\\from events\n\\join users on user_id = id\n\\select region\n\\select sum(amount) as total"
     "\n\\group region\n\\order region", [["us", 50.5]]),
    ("count-star", "\\from events\n\\select count(*) as n", [[4]]),
    ("count-column", "\\from events\n\\select count(user_id) as n", [[3]]),
    ("window-frame-rows",
     "\\from events\n\\select id\n\\select sum(amount) over (order by id"
     " rows between unbounded preceding and current row) as running\n\\order id",
     [[1, 10.5], [2, 40.5], [3, 80.5], [4, 80.5]]),
    ("rank-over", "\\from events\n\\select id\n\\select rank() over (order by amount desc) as r\n\\order id",
     [[1, 3], [2, 2], [3, 1], [4, 4]]),
    ("cte", "\\with r\n  \\from events\n  \\select id\n  \\where amount > 20"
     "\n\\from r\n\\select id\n\\order id", [[2], [3]]),
    ("in-subquery",
     "\\from events\n\\select id\n\\where user_id in (\\from users \\select id \\where region = 'us')"
     "\n\\order id", [[1], [3], [4]]),
    ("lateral-top-one",
     "\\from users\n\\join lateral (\\from events \\where user_id = users.id"
     " \\order amount desc \\limit 1) as top on users.id = top.user_id"
     "\n\\select users.id\n\\select top.amount\n\\order users.id",
     [[1, 10.5], [4, 40]]),
    ("union-all", "\\from events\n\\select kind\n\\union all (\\from events \\select kind)\n\\order kind",
     [["CLICK"], ["CLICK"], ["click"], ["click"], ["purchase"], ["purchase"], ["view"], ["view"]]),
    ("union-dedup", "\\from events\n\\select kind\n\\union (\\from events \\select kind)\n\\order kind",
     [["CLICK"], ["click"], ["purchase"], ["view"]]),
    ("distinct", "\\from events\n\\select kind\n\\distinct\n\\order kind",
     [["CLICK"], ["click"], ["purchase"], ["view"]]),
    ("limit-desc", "\\from events\n\\select id\n\\order id desc\n\\limit 2", [[4], [3]]),
]

PARITY_BY_NAME = {name: (doc, expected) for name, doc, expected in PARITY}

# The §4.3 ✗ cells for the live-D1 column: `_IBIS_TRANSLATE` and
# `_IBIS_REGEX_SEARCH` are UDFs Cloudflare's SQLite does not have, so the
# engine must refuse them — and `capabilities_for` must already have hidden
# them, so the palette never offers what this loop proves fails.
D1_REFUSED = {"translate", "regex", "regex-negated"}

# Real, measured drift, not harness noise: ibis's own SQLite connections set
# `PRAGMA case_sensitive_like = ON` at connect, and DuckDB's `LIKE` is
# case-sensitive too. Live D1 is reached over stateless HTTP — the app has no
# connection to put that pragma on, so each request runs on stock SQLite's
# default: `LIKE` matches case-insensitively. `like 'c%'` matches `CLICK`
# there. `ilike` is unaffected (ibis lowers it to `LOWER … LIKE`), which is
# why it stays a plain parity cell.
D1_LIKE_DIFFERS = {
    "like-prefix": [[1], [3]],
    "like-underscore": [[1], [3]],
}


@pytest.mark.parametrize("cell", sorted(PARITY_BY_NAME))
def test_every_backend_returns_the_same_rows(backends, cell):
    doc, expected = PARITY_BY_NAME[cell]
    for name, source in backends:
        if name == "d1-live":
            if cell in D1_REFUSED:
                caps = capabilities_for(source)
                if cell == "translate":
                    assert "translate" not in caps["functions"]["string"]
                else:
                    assert "~" not in caps["operators"] and "!~" not in caps["operators"]
                with pytest.raises(D1Error, match="no such function"):
                    rows(doc, source)
                continue
            if cell in D1_LIKE_DIFFERS:
                assert rows(doc, source) == D1_LIKE_DIFFERS[cell], f"{cell} drifted on {name}"
                continue
        assert rows(doc, source) == expected, f"{cell} drifted on {name}"


def test_sqlite_url_accessors_execute_where_duckdb_pg_and_d1_cannot(
        duckdb_source, sqlite_source, d1_live_source, postgres_source):
    doc = "\\from events\n\\select id\n\\select url_host(url)\n\\order id"
    # The probe says what the compilers can render; the offer list follows it.
    assert any(fn.startswith("url_") for fn in capabilities_for(sqlite_source)["functions"]["string"])
    # D1-live offers none (its SQLite has no `_IBIS_EXTRACT_*` UDFs) and the
    # API-side engine says so in the error envelope the app surfaces.
    assert not any(fn.startswith("url_") for fn in capabilities_for(d1_live_source)["functions"]["string"])
    with pytest.raises(D1Error, match="no such function"):
        rows(doc, d1_live_source)
    for source in (duckdb_source, postgres_source):
        if source is None:
            continue
        assert not any(fn.startswith("url_") for fn in capabilities_for(source)["functions"]["string"])
        with pytest.raises(Exception, match="ExtractHost|url_host"):
            rows(doc, source)
    assert rows(doc, sqlite_source) == [
        [1, "acme.test"], [2, "other.test"], [3, None], [4, None],
    ]


@pytest.mark.parametrize(
    ("dialect", "needles"),
    [
        ("postgres", ["ILIKE", "BETWEEN", "IS NULL", "IN (", "NOT"]),
        ("duckdb", ["ILIKE", "BETWEEN", "IS NULL", "IN ("]),
        ("sqlite", ["LOWER", "LIKE", "BETWEEN", "IS NULL", "IN ("]),
    ],
)
def test_the_new_predicates_render_as_real_sql(duckdb_source, dialect, needles):
    doc = (
        "\\from events\n\\select id\n\\where kind ilike 'click'"
        " and amount between 1 and 2 and url is null or user_id not in (5, 6)"
    )
    expr = build(duckdb_source.con, payload_from_ast(parse_query(doc)), tables=tables_of(duckdb_source))
    sql = compile_sql(expr, dialect).upper()
    for needle in needles:
        assert needle.upper() in sql, f"{needle} missing from the {dialect} rendering"


def test_session_run_executes_the_surface_end_to_end(duckdb_source, sqlite_source, d1_live_source,
                                                     postgres_source, tmp_path):
    """`Session.run` — the IDE's real seam — over each backend's registry
    source, the live-D1 one included (it dispatches to `execute_remote`)."""
    from d8r.tui.session import Session

    for source in (duckdb_source, sqlite_source, d1_live_source, postgres_source):
        if source is None:
            continue
        session = Session({source.id: source}, data_dir=tmp_path / source.id)
        session.set_active(source.id)
        outcome = session.run(
            "\\from events\n\\where kind ilike 'CLICK' or amount is null\n\\order id"
        )
        assert outcome.ok, outcome.status
        assert [row[0] for row in outcome.rows] == [1, 3, 4]
        assert not outcome.error
