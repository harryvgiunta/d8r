"""Datasource registry for D8R's Python engine.

A datasource is a named connection carrying its own dataset schemas. `demo`
is the bundled Parquet directory executed on a real in-process DuckDB; other
bundled entries are vendor-shaped mock schemas mirrored into DuckDB. Explicitly
added D1 sources either use a local SQLite snapshot or Cloudflare's live API.

Every bundled path resolves from this module's own file, never the process
working directory, so the engine behaves the same however it is launched.
Nothing is opened implicitly: a D1 snapshot enters the registry only when a
caller names its path (`add_sqlite_source`).

The dialect is an independent compile target: SQL renders via
`ibis.to_sql(expr, dialect=...)`, so any query can be shown as any supported
database would receive it — whether or not a live connection of that name
exists. Each datasource suggests a default dialect (`duckdb` for demo, the
vendor's own for mocks); callers may override per query.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import ibis
import pyarrow as pa
import pyarrow.parquet as pq

from d8r.query.functions import SCALAR_FUNCTIONS

from .d1api import CloudflareD1, schema_connection
from .expression import PayloadError, compile_sql

ENGINE_DIR = Path(__file__).resolve().parent
DATA_DIR = ENGINE_DIR / "data"

DEMO_ID = "demo"

# Hand-written catalog for the demo datasource; a dataset with no entry falls
# back to its own name.
CATALOG = {
    "events": "Web analytics events: one row per tracked event.",
    "users": "Signed-up users: region, join date, activity score.",
}

# ---------------------------------------------------------------------------
# Capability set (single source; the UI renders it per datasource).
# ---------------------------------------------------------------------------

CAPABILITIES: dict = {
    "backend": "duckdb",
    "aggregates": ["sum", "avg", "count", "min", "max"],
    # Type-keyed scalar functions: dtype family -> usable functions. The
    # front end filters completion by the column's dtype, so a timestamp
    # offers date parts and a float offers aggregates.
    "functions": {
        "timestamp": ["year", "month", "day", "quarter", "hour", "minute", "second"],
        "date": ["year", "month", "day", "quarter"],
        "time": ["hour", "minute", "second"],
        "string": [fn for fn in SCALAR_FUNCTIONS if fn != "string"],
        "any": ["string"],
    },
    "operators": ["=", "!=", ">", ">=", "<", "<=", "like", "~", "!~", "in", "not in"],
    # Rank-style window functions usable as `<fn>() over ( … )` in a select.
    "windowFunctions": ["rank", "dense_rank", "row_number"],
    "supports": {
        "groupBy": True,
        "orderBy": True,
        "limit": True,
        "distinct": True,
        "like": True,
        "case": True,
        "window": True,
        "cte": True,
        # The clauses the six SQL features above ibis landed as: inline
        # subqueries, lateral joins, frame bounds, regex, and — the two writes
        # — temp tables and transactions. Savepoints are the engine's own gift:
        # SQLite has them, DuckDB keeps whole transactions only.
        "subquery": True,
        "lateral": True,
        "frame": True,
        "regex": True,
        "temp": True,
        "transactions": True,
        "savepoints": False,
    },
}


def capabilities_for(source: "DataSource") -> dict:
    """The capability set as advertised for one datasource.

    Mock connections advertise the same ibis-translatable surface; only the
    `backend` label differs so the UI states honestly what it is talking to.
    A D1 source is a real SQLite engine, not a mock, and says which flavor: a
    local snapshot, or a live Cloudflare D1 reached over its API. A live D1 is
    reached over stateless HTTP, so it can neither keep a transaction nor hold a
    temp table; a snapshot runs in-process and can do both, savepoints included.
    """
    if source.kind == "demo":
        return CAPABILITIES
    if source.kind == "d1":
        return {
            **CAPABILITIES,
            "backend": "sqlite (D1 snapshot)",
            "supports": {**CAPABILITIES["supports"], "savepoints": True},
        }
    if source.kind == "d1-live":
        return {
            **CAPABILITIES,
            "backend": "sqlite (Cloudflare D1)",
            # These SQLite translations compile to Python UDFs (`_IBIS_*`) that
            # only the local Ibis backend registers; Cloudflare D1 has no such
            # functions. The rest — capitalize included, which compiles to pure
            # SQL (UPPER/SUBSTRING) — D1 runs natively.
            "functions": {
                **CAPABILITIES["functions"],
                "string": [
                    fn for fn in CAPABILITIES["functions"]["string"]
                    if fn not in {"reverse", "repeat", "lpad", "rpad"}
                ],
            },
            # `~`/`!~` compile to `_IBIS_REGEX_SEARCH`, so the regex operators
            # go with the regex flag: the palette reads this operator list.
            "operators": [
                op for op in CAPABILITIES["operators"] if op not in {"~", "!~"}
            ],
            "supports": {
                **CAPABILITIES["supports"],
                "regex": False,
                "temp": False,
                "transactions": False,
            },
        }
    return {**CAPABILITIES, "backend": f"{source.dialect} (mock)"}


# ---------------------------------------------------------------------------
# Compile targets. `compiles: False` means ibis bundles the compiler but this
# build cannot render it offline (pyspark needs a live session config); the
# front end shows the entry as known-but-uncompilable rather than hiding it.
# ---------------------------------------------------------------------------

DIALECTS: tuple[dict, ...] = (
    {"name": "duckdb", "label": "DuckDB", "compiles": True},
    {"name": "athena", "label": "Amazon Athena", "compiles": True},
    {"name": "bigquery", "label": "BigQuery", "compiles": True},
    {"name": "clickhouse", "label": "ClickHouse", "compiles": True},
    {"name": "databricks", "label": "Databricks", "compiles": True},
    {"name": "datafusion", "label": "DataFusion", "compiles": True},
    {"name": "druid", "label": "Apache Druid", "compiles": True},
    {"name": "exasol", "label": "Exasol", "compiles": True},
    {"name": "flink", "label": "Apache Flink", "compiles": True},
    {"name": "impala", "label": "Impala", "compiles": True},
    {"name": "materialize", "label": "Materialize", "compiles": True},
    {"name": "mssql", "label": "SQL Server", "compiles": True},
    {"name": "mysql", "label": "MySQL", "compiles": True},
    {"name": "oracle", "label": "Oracle", "compiles": True},
    {"name": "postgres", "label": "PostgreSQL", "compiles": True},
    {"name": "pyspark", "label": "PySpark", "compiles": False},
    {"name": "risingwave", "label": "RisingWave", "compiles": True},
    {"name": "snowflake", "label": "Snowflake", "compiles": True},
    {"name": "sqlite", "label": "SQLite", "compiles": True},
    {"name": "trino", "label": "Trino", "compiles": True},
)

DIALECT_BY_NAME: dict[str, dict] = {d["name"]: d for d in DIALECTS}


# ---------------------------------------------------------------------------
# Mock datasets: vendor-shaped schemas with deterministic rows (pure
# arithmetic — same rule as make_data.py, so regeneration is byte-identical).
# ---------------------------------------------------------------------------

ORDER_STATUSES = ["new", "paid", "shipped", "cancelled"]
CUSTOMER_REGIONS = ["emea", "amer", "apac"]
PRODUCT_CATEGORIES = ["tools", "widgets", "parts", "media"]
CAMPAIGN_CHANNELS = ["search", "social", "email", "affiliate"]
SESSION_SOURCES = ["direct", "organic", "paid", "referral"]
CONVERSION_GOALS = ["signup", "trial", "purchase"]


def _ts(start: dt.datetime, step: dt.timedelta, k: int) -> dt.datetime:
    return start + step * k


def _ts_col(values: list[dt.datetime]) -> pa.Array:
    return pa.array(values, type=pa.timestamp("us"))


def customers_table() -> pa.Table:
    n = 20
    return pa.table(
        {
            "customer_id": pa.array(list(range(1, n + 1)), type=pa.int64()),
            "name": pa.array([f"cust_{k + 1:02d}" for k in range(n)], type=pa.string()),
            "region": pa.array([CUSTOMER_REGIONS[k % 3] for k in range(n)], type=pa.string()),
            "created_at": _ts_col([_ts(dt.datetime(2022, 1, 3), dt.timedelta(days=23), k) for k in range(n)]),
        }
    )


def orders_table() -> pa.Table:
    n = 120
    return pa.table(
        {
            "order_id": pa.array(list(range(1, n + 1)), type=pa.int64()),
            "customer_id": pa.array([1 + (k * 7) % 20 for k in range(n)], type=pa.int64()),
            "status": pa.array([ORDER_STATUSES[k % 4] for k in range(n)], type=pa.string()),
            "amount": pa.array([round((k % 17) * 2.25 + 1.5, 2) for k in range(n)], type=pa.float64()),
            "placed_at": _ts_col([_ts(dt.datetime(2024, 2, 1), dt.timedelta(hours=19), k) for k in range(n)]),
        }
    )


def products_table() -> pa.Table:
    n = 30
    return pa.table(
        {
            "product_id": pa.array(list(range(1, n + 1)), type=pa.int64()),
            "sku": pa.array([f"SKU-{k + 1:03d}" for k in range(n)], type=pa.string()),
            "category": pa.array([PRODUCT_CATEGORIES[k % 4] for k in range(n)], type=pa.string()),
            "price": pa.array([round((k % 11) * 3.1 + 0.5, 2) for k in range(n)], type=pa.float64()),
            "stock": pa.array([(k * 29) % 100 for k in range(n)], type=pa.int64()),
        }
    )


def reviews_table() -> pa.Table:
    n = 90
    return pa.table(
        {
            "review_id": pa.array(list(range(1, n + 1)), type=pa.int64()),
            "product_id": pa.array([1 + (k * 5) % 30 for k in range(n)], type=pa.int64()),
            "rating": pa.array([1 + (k * 3) % 5 for k in range(n)], type=pa.int64()),
            "body": pa.array([f"review {k + 1:03d}" for k in range(n)], type=pa.string()),
            "posted_at": _ts_col([_ts(dt.datetime(2024, 3, 5), dt.timedelta(hours=7), k) for k in range(n)]),
        }
    )


def campaigns_table() -> pa.Table:
    n = 12
    return pa.table(
        {
            "campaign_id": pa.array(list(range(1, n + 1)), type=pa.int64()),
            "channel": pa.array([CAMPAIGN_CHANNELS[k % 4] for k in range(n)], type=pa.string()),
            "budget": pa.array([float((k + 1) * 250) for k in range(n)], type=pa.float64()),
            "launched_at": _ts_col([_ts(dt.datetime(2024, 1, 15), dt.timedelta(days=30), k) for k in range(n)]),
        }
    )


def spend_log_table() -> pa.Table:
    n = 60
    return pa.table(
        {
            "log_id": pa.array(list(range(1, n + 1)), type=pa.int64()),
            "campaign_id": pa.array([1 + (k * 3) % 12 for k in range(n)], type=pa.int64()),
            "day": _ts_col([_ts(dt.datetime(2024, 2, 1), dt.timedelta(days=2), k) for k in range(n)]),
            "spend": pa.array([round(((k * 7) % 40) * 1.75 + 0.4, 2) for k in range(n)], type=pa.float64()),
        }
    )


def web_sessions_table() -> pa.Table:
    n = 80
    return pa.table(
        {
            "session_id": pa.array([f"sess-{k + 1:04d}" for k in range(n)], type=pa.string()),
            "visitor_id": pa.array([f"vis-{(k * 13) % 40 + 1:03d}" for k in range(n)], type=pa.string()),
            "source": pa.array([SESSION_SOURCES[k % 4] for k in range(n)], type=pa.string()),
            "started_at": _ts_col([_ts(dt.datetime(2024, 5, 1), dt.timedelta(minutes=13), k) for k in range(n)]),
            "pages": pa.array([1 + (k * 5) % 9 for k in range(n)], type=pa.int64()),
        }
    )


def conversions_table() -> pa.Table:
    n = 40
    return pa.table(
        {
            "conversion_id": pa.array(list(range(1, n + 1)), type=pa.int64()),
            "session_id": pa.array([f"sess-{(k * 2) % 80 + 1:04d}" for k in range(n)], type=pa.string()),
            "goal": pa.array([CONVERSION_GOALS[k % 3] for k in range(n)], type=pa.string()),
            "value": pa.array([round(((k * 11) % 60) * 1.9 + 2.0, 2) for k in range(n)], type=pa.float64()),
        }
    )


@dataclass(frozen=True)
class MockTableSpec:
    name: str
    doc: str
    build: Callable[[], pa.Table]


@dataclass(frozen=True)
class MockSpec:
    id: str
    display: str
    doc: str
    dialect: str
    tables: tuple[MockTableSpec, ...]


MOCKS: tuple[MockSpec, ...] = (
    MockSpec(
        id="postgres",
        display="Acme Commerce · PostgreSQL (mock)",
        doc="Mock Postgres OLTP store: customer orders and accounts.",
        dialect="postgres",
        tables=(
            MockTableSpec("orders", "One row per customer order: status, amount, placement time.", orders_table),
            MockTableSpec("customers", "Customer accounts: region and signup date.", customers_table),
        ),
    ),
    MockSpec(
        id="mysql",
        display="Catalog DB · MySQL (mock)",
        doc="Mock MySQL catalog service: products and customer reviews.",
        dialect="mysql",
        tables=(
            MockTableSpec("products", "Catalog products: SKU, category, price, stock.", products_table),
            MockTableSpec("reviews", "Product reviews: rating, body, posted time.", reviews_table),
        ),
    ),
    MockSpec(
        id="snowflake",
        display="Marketing DW · Snowflake (mock)",
        doc="Mock Snowflake warehouse: marketing campaigns and daily spend.",
        dialect="snowflake",
        tables=(
            MockTableSpec("campaigns", "Marketing campaigns: channel, budget, launch date.", campaigns_table),
            MockTableSpec("spend_log", "Daily campaign spend log.", spend_log_table),
        ),
    ),
    MockSpec(
        id="bigquery",
        display="Web Analytics · BigQuery (mock)",
        doc="Mock BigQuery analytics dataset: sessions and conversions.",
        dialect="bigquery",
        tables=(
            MockTableSpec("web_sessions", "Web sessions: source, start time, pages viewed.", web_sessions_table),
            MockTableSpec("conversions", "Goal conversions tied to sessions.", conversions_table),
        ),
    ),
)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass
class DataSource:
    id: str
    display: str
    doc: str
    kind: str  # "demo" | "mock" | "d1" | "d1-live"
    dialect: str  # suggested default compile dialect
    dir: Path
    con: object = None
    datasets: dict[str, dict] = field(default_factory=dict)
    # A live Cloudflare D1 connection (d8r.engine.d1api.CloudflareD1) when
    # kind == "d1-live". Its presence tells the caller to ship compiled SQLite
    # SQL over the D1 HTTP API instead of executing on a local `con`.
    d1: object = None


def type_name(dtype) -> str:
    """The public label for a column dtype (e.g. `timestamp`, not `timestamp(6)`).

    One rule for every surface that names a type: the schema pane, the
    generated fixtures and the column contract all read the same way.
    """
    return "timestamp" if dtype.is_timestamp() else str(dtype)


def dialect_for(source: DataSource, name: str | None = None) -> str:
    """The dialect a payload for `source` compiles to; `None` is its default.

    A name this build does not know, or one it knows but cannot render offline
    (`compiles: False`, i.e. pyspark), is the user's mistake, so it raises
    `PayloadError` before any expression is built.
    """
    chosen = name or source.dialect
    spec = DIALECT_BY_NAME.get(chosen)
    if spec is None:
        raise PayloadError(f"unknown dialect: {chosen}")
    if not spec["compiles"]:
        raise PayloadError(f"dialect {chosen} does not compile in this build")
    return chosen


def column_values(source: DataSource, dataset: str, column: str, limit: int = 20) -> list[str]:
    """One column's distinct non-null values, sorted — the value-list surface.

    Runs on whichever engine the source owns: its own connection, or, for a
    live D1 source, SQLite SQL posted through the D1 API.
    """
    entry = source.datasets.get(dataset)
    if entry is None:
        raise PayloadError(f"unknown dataset: {dataset}")
    table = entry["table"]
    if column not in table.columns:
        raise PayloadError(f"unknown column: {column}")
    expr = (
        table.filter(table[column].notnull())
        .select(table[column])
        .distinct()
        .order_by(column)
        .limit(limit)
    )
    if source.d1 is not None:
        _, rows = source.d1.raw(compile_sql(expr, dialect="sqlite"))
        return [str(row[0]) for row in rows]
    return [str(value) for value in expr.execute().iloc[:, 0]]


def write_mock_data(root: Path) -> None:
    """(Re)write every mock datasource's Parquet mirror under `root/<id>/`.

    Deterministic builders — regenerating always produces byte-identical
    files, per the repo's data conventions.
    """
    for spec in MOCKS:
        out = root / spec.id
        out.mkdir(parents=True, exist_ok=True)
        for table in spec.tables:
            pq.write_table(table.build(), out / f"{table.name}.parquet")


def ensure_mock_data(root: Path) -> None:
    """Write mock Parquet mirrors only when missing (runtime convenience)."""
    missing = any(
        not (root / spec.id / f"{table.name}.parquet").exists() for spec in MOCKS for table in spec.tables
    )
    if missing:
        write_mock_data(root)


def _ingest(source: DataSource, catalog: dict[str, str]) -> DataSource:
    """Register every Parquet file in the source's directory as a dataset."""
    source.con = ibis.duckdb.connect()
    paths = sorted(source.dir.glob("*.parquet"))
    if not paths:
        raise RuntimeError(f"datasource {source.id!r} has no datasets in {source.dir}")
    for path in paths:
        name = path.stem
        source.con.create_table(name, source.con.read_parquet(str(path)))
        table = source.con.table(name)
        source.datasets[name] = {
            "table": table,
            "doc": catalog.get(name, name),
            "rows": int(table.count().execute()),
        }
    return source


def load(
    root: Path | None = None,
    demo_catalog: dict[str, str] | None = None,
) -> dict[str, DataSource]:
    """Build the registry: `demo` first, then every mock, in declaration order.

    Both arguments default to the engine's own bundled tree and catalog, so
    `load()` is the whole entry point: no path, no working directory.
    Mock mirrors are written on demand when missing, so a fresh checkout
    serves them without a manual `make_data.py` run.
    """
    root = Path(root) if root is not None else DATA_DIR
    demo_catalog = CATALOG if demo_catalog is None else demo_catalog
    ensure_mock_data(root)
    sources: dict[str, DataSource] = {}
    sources[DEMO_ID] = _ingest(
        DataSource(
            id=DEMO_ID,
            display="Local demo · DuckDB + Parquet",
            doc="Bundled deterministic datasets on a real in-process DuckDB.",
            kind="demo",
            dialect="duckdb",
            dir=root,
        ),
        demo_catalog,
    )
    for spec in MOCKS:
        sources[spec.id] = _ingest(
            DataSource(
                id=spec.id,
                display=spec.display,
                doc=spec.doc,
                kind="mock",
                dialect=spec.dialect,
                dir=root / spec.id,
            ),
            {t.name: t.doc for t in spec.tables},
        )
    return sources


# ---------------------------------------------------------------------------
# Cloudflare D1 sources — connected at runtime; persistence belongs to storage.
#
# A D1 database *is* SQLite: Cloudflare runs a real SQLite engine and its own
# D1 console introspects it through `sqlite_schema` / `pragma_*`. Two shapes:
#
#   * local snapshot (`kind="d1"`): a SQLite file exported out-of-band
#     (`wrangler d1 execute --local`), opened in-process through ibis's SQLite
#     backend — same engine, no network.
#   * live (`kind="d1-live"`): the database reached over Cloudflare's HTTPS
#     API with the user's own credentials. Real schemas are pulled into unbound
#     ibis tables so payloads compile to SQLite SQL exactly like every other
#     source; the SQL is then run back through the API (`d8r.engine.execute`).
#
# A snapshot is the user's own file (outside `d8r/engine/data/`, never
# regenerated). Live clients hold credentials in memory for requests; explicit
# Add in the TUI saves the profile and token through the local storage layer.
# ---------------------------------------------------------------------------


def _ingest_sqlite(source: DataSource) -> DataSource:
    """Attach a real in-process SQLite connection to a local database file.

    Every user table/view becomes a dataset. Internal `sqlite_*` and
    Cloudflare `_cf_*` objects are skipped — the exact filter D1's own console
    applies — so the explorer shows the user's schema, not the engine's plumbing.
    """
    if not source.dir.is_file():
        raise FileNotFoundError(f"no SQLite database at {source.dir}")
    # A query may run on any thread. DuckDB tolerates a connection crossing
    # threads; stdlib sqlite3 refuses by default, so the handle is opened with
    # the thread check off — the same single shared, in-process connection the
    # demo source already relies on. `isolation_level=None` is autocommit: the
    # driver stops wrapping statements in transactions of its own, which leaves
    # `\begin`/`\savepoint`/`\commit` (d8r.engine.tx) as the only transaction
    # control on the connection. The snapshot is opened read-only from the
    # user's point of view (queries never write; a temp table lives in SQLite's
    # temp schema, not in the database file).
    import sqlite3

    raw = sqlite3.connect(str(source.dir), check_same_thread=False, isolation_level=None)
    try:
        source.con = ibis.sqlite.from_connection(raw)
        for name in source.con.list_tables():
            if name.startswith("sqlite_") or name.startswith("_cf_"):
                continue
            table = source.con.table(name)
            source.datasets[name] = {
                "table": table,
                "doc": f"{name} · {source.dir.name}",
                "rows": int(table.count().execute()),
            }
        if not source.datasets:
            raise RuntimeError(f"database {source.dir} has no user tables")
    except Exception:
        raw.close()
        raise
    return source


def add_sqlite_source(source_id: str, path: str, display: str | None = None) -> DataSource:
    """Build (but do not register) a D1/SQLite datasource from an explicit path.

    The path is always the caller's — nothing here falls back to a bundled
    snapshot, so a source exists only because someone named the file.
    """
    if not (path or "").strip():
        raise ValueError("a database path is required")
    db = Path(path).expanduser()
    label = (display or "").strip() or f"D1 snapshot · {db.stem}"
    return _ingest_sqlite(
        DataSource(
            id=source_id,
            display=label,
            doc=f"Local SQLite snapshot · {db.name}",
            kind="d1",
            dialect="sqlite",
            dir=db,
        )
    )

def add_d1_live_source(
    source_id: str,
    account_id: str,
    api_token: str,
    database: str,
    display: str | None = None,
    client: "CloudflareD1 | None" = None,
) -> DataSource:
    """Build (but do not register) a *live* Cloudflare D1 datasource.

    The database is reached over its HTTPS API, never opened locally. Its real
    schemas are pulled once into unbound ibis tables — enough for the explorer
    and for compiling every payload to SQLite SQL; the SQL is then run back
    through the API (see `d8r.engine.execute`). A bad token or unknown database
    raises `D1Error` during introspection, before anything registers.
    """
    d1 = client or CloudflareD1(account_id=account_id, api_token=api_token, database=database)
    try:
        d1.resolve()
        schemas = d1.schemas()
        counts = d1.row_counts(list(schemas))
        label = (display or "").strip() or f"D1 · {database}"
        con = schema_connection(schemas)
        source = DataSource(
            id=source_id,
            display=label,
            doc=f"Cloudflare D1 · {d1.database_uuid}",
            kind="d1-live",
            dialect="sqlite",
            dir=Path(database),
            con=con,
            d1=d1,
        )
        for name in schemas:
            source.datasets[name] = {
                "table": con.table(name),
                "doc": f"{name} · {database}",
                "rows": counts.get(name, 0),
            }
        return source
    except Exception:
        d1.close()
        raise
