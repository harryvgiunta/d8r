"""Datasource registry for D8R's Python engine.

A datasource is a named connection carrying its own dataset schemas. `demo`
is the bundled Parquet directory executed on a real in-process DuckDB; other
bundled entries are vendor-shaped mock schemas mirrored into DuckDB. Explicitly
added sources use a local SQLite snapshot, Cloudflare's live D1 API, or PostgreSQL.

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
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import ibis
import ibis.expr.operations as ops
import pyarrow as pa
import pyarrow.parquet as pq

from d8r.query.functions import DUCKDB_UNRENDERABLE, SCALAR_FUNCTIONS, URL_ACCESSORS

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
    # offers date parts and a float offers aggregates. The string list is
    # this backend's honest truth: the URL accessors and `convert_base`
    # compile to nothing here (see `DUCKDB_UNRENDERABLE` in the function
    # catalog), so they are absent until a source can actually run them.
    "functions": {
        "timestamp": ["year", "month", "day", "quarter", "hour", "minute", "second"],
        "date": ["year", "month", "day", "quarter"],
        "time": ["hour", "minute", "second"],
        "string": [
            fn for fn in SCALAR_FUNCTIONS
            if fn not in {"string", "coalesce", "nullif"} and fn not in DUCKDB_UNRENDERABLE
        ],
        # `coalesce`/`nullif` are cross-type: any dtype, any backend (both
        # compile to plain SQL everywhere this app reaches).
        "any": ["string", "coalesce", "nullif"],
    },
    "operators": [
        "=", "!=", ">", ">=", "<", "<=", "like", "ilike", "~", "!~",
        "in", "not in", "between", "is null", "is not null",
    ],
    # Rank-style window functions usable as `<fn>() over ( … )` in a select.
    "windowFunctions": ["rank", "dense_rank", "row_number"],
    "supports": {
        "groupBy": True,
        "orderBy": True,
        "limit": True,
        "distinct": True,
        "like": True,
        "ilike": True,
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
        # ibis-level operation surface, duckdb truth (live-probed per source;
        # see `_probe_flags`). Conservative False defaults — a source only
        # claims what its compiler can actually render.
        "quantile": True,
        "asofJoin": True,
        "sampling": True,
        "samplingSeed": True,
        "unnest": True,
    },
}

# The probe surface: flag -> ibis operation class whose compilation rule a
# connection must have (`con.has_operation`). The flags not listed here —
# `asofJoin` and `samplingSeed` — have no operation class in ibis 12 (an
# as-of join lowers to an ordinary Join with a marker; sampling folds the seed
# into `Sample`), so they are probed by compiling a representative expression
# against the backend's own compiler. `has_operation` is the backend's own
# declaration and never raises; the compile probes are wrapped anyway.
_PROBE_OPS: dict[str, str] = {
    "quantile": "Quantile",
    "unnest": "TableUnnest",
    "ilike": "StringSQLILike",
    "sampling": "Sample",
}

_PROBE_TABLE = {"a": "int64", "s": "string", "ts": "timestamp", "arr": "array<int64>"}
_PROBE_JOIN = {"k": "int64", "ts": "timestamp"}


# Probes are cheap (offline compiles) but `supports()` runs per UI check, so
# each connection's answers are memoized. Connections live as long as their
# source; if a connection cannot be weak-referenced the probe just reruns.
_PROBE_CACHE: "weakref.WeakKeyDictionary[object, dict]" = weakref.WeakKeyDictionary()


def _probe_compile(con, make) -> bool:
    """Whether `con` can build and render `make()` (offline, no execution).

    The expression is built inside the guard: some backends refuse certain
    operations at construction (SQLite rejects ASOF joins and seeded samples
    before any SQL is compiled), and a refusal is a refusal.
    """
    try:
        compile_sql(make(), dialect=getattr(con, "name", None))
    except Exception:
        return False
    return True


def _probe_flags(con) -> dict:
    """Derive the operation-support flags from one live connection.

    `has_operation` answers for the backend's own compiler; the two flags
    with no operation class of their own (`asofJoin`, `samplingSeed`) are
    compile probes. The probe tables are bound to `con` (a `con.sql` stub
    with a declared schema — never executed) so per-backend construction
    checks, like SQLite refusing an ASOF join or a seeded sample outright,
    count as refusals too. On probe failure the flag is False — the
    conservative direction.
    """
    flags = {flag: False for flag in (*_PROBE_OPS, "asofJoin", "samplingSeed")}
    for flag, op_name in _PROBE_OPS.items():
        try:
            flags[flag] = bool(con.has_operation(getattr(ops, op_name)))
        except Exception:
            flags[flag] = False
    try:
        t = con.sql("SELECT 1", schema=_PROBE_TABLE)
        left = con.sql("SELECT 1", schema=_PROBE_JOIN)
        right = con.sql("SELECT 2", schema=_PROBE_JOIN)
    except Exception:
        return flags
    flags["samplingSeed"] = _probe_compile(con, lambda: t.sample(0.1, seed=7))
    flags["asofJoin"] = _probe_compile(
        con, lambda: left.asof_join(right, "k", predicates=[left.ts < right.ts])
    )
    return flags


def _unrenderable_on(con) -> frozenset[str]:
    """Scalar functions this connection's compiler cannot render at all.

    Measured, not declared: each candidate call is compiled (offline, without
    executing) against the connection's own backend. A connection that cannot
    compile anything (a stub, a broken handle) has every candidate unmeasured,
    and honest conservatism is not to advertise what cannot be verified.
    """
    calls = {
        "translate": lambda: t.s.translate("a", "b"),
        "levenshtein": lambda: t.s.levenshtein("x"),
        "as_date": lambda: t.s.as_date("%Y-%m-%d"),
        "as_time": lambda: t.s.as_time("%H:%M:%S"),
        "as_timestamp": lambda: t.s.as_timestamp("%Y-%m-%d %H:%M:%S"),
        "convert_base": lambda: t.s.convert_base(16, 10),
        "url_protocol": lambda: t.s.protocol(),
        "url_host": lambda: t.s.host(),
        "url_path": lambda: t.s.path(),
        "url_query": lambda: t.s.query(),
        "url_fragment": lambda: t.s.fragment(),
    }
    try:
        t = con.sql("SELECT 1", schema={"a": "int64", "s": "string"})
    except Exception:
        return frozenset(calls)
    dead = set()
    for name, make in calls.items():
        if not _probe_compile(con, lambda make=make: make().name("x")):
            dead.add(name)
    return frozenset(dead)

# Functions ibis's PostgreSQL compiler renders but a server only answers
# when an extension provides them (fuzzystrmatch owns levenshtein). A compile
# probe cannot see server state; the catalog can, and it is queried once per
# connection at first capability call — introspection like schema discovery,
# never a row scan, never an extension install.
_PG_EXTENSION_FUNCTIONS = frozenset({"levenshtein"})


def _pg_extension_function_gaps(con) -> frozenset[str]:
    try:
        with con.con.cursor() as cursor:
            rows = cursor.execute(
                "select proname from pg_proc where proname = any(%s)",
                (sorted(_PG_EXTENSION_FUNCTIONS),),
            ).fetchall()
    except Exception:
        # Catalog unreadable: conservative honesty is not to advertise.
        return _PG_EXTENSION_FUNCTIONS
    have = {row[0] for row in rows}
    return frozenset(n for n in _PG_EXTENSION_FUNCTIONS if n not in have)


def _probes_for(con) -> dict:
    """`_probe_flags` + `_unrenderable_on` for one connection, memoized."""
    try:
        cached = _PROBE_CACHE.get(con)
    except TypeError:  # unhashable/unsupported connection object
        cached = None
    if cached is not None:
        return cached
    dead = _unrenderable_on(con)
    if getattr(con, "name", None) == "postgres":
        dead = dead | _pg_extension_function_gaps(con)
    probed = {**_probe_flags(con), "dead": dead}
    try:
        _PROBE_CACHE[con] = probed
    except TypeError:
        pass
    return probed


def capabilities_for(source: "DataSource") -> dict:
    """The capability set as advertised for one datasource.

    Mock connections run on DuckDB and are live-probed like the demo — same
    compiler, same truth; only the `backend` label differs so the UI states
    honestly what it is talking to. A D1 source is a real SQLite engine, not
    a mock, and says which flavor: a local snapshot, or a live Cloudflare D1
    reached over its API. A live D1 is reached over stateless HTTP, so it can
    neither keep a transaction nor hold a temp table; a snapshot runs
    in-process and can do both, savepoints included.

    The operation-support flags (`quantile`, `asofJoin`, `sampling`,
    `samplingSeed`, `unnest`, `ilike`) come from `_probe_flags` wherever a
    real connection exists — demo, mocks, snapshots, and PostgreSQL all
    answer for their own compilers. Live D1 has no connection to probe (the
    SQL runs remotely over HTTP) and drops honestly what Cloudflare cannot
    run: no quantile rule in SQLite SQL, no as-of join, no seeded sampling,
    no native unnest — while `ilike` (pure `LOWER … LIKE`) and unseeded
    sampling (`WHERE random() <= f`) stay.
    """
    if source.kind == "d1-live":
        return {
            **CAPABILITIES,
            "backend": "sqlite (Cloudflare D1)",
            # These SQLite translations compile to Python UDFs (`_IBIS_*`) or
            # to rules plain Cloudflare SQLite has never heard of; D1 has no
            # such functions. The rest — capitalize included, which compiles
            # to pure SQL (UPPER/SUBSTRING) — D1 runs natively. (`translate`
            # rides `_IBIS_TRANSLATE`; the URL accessors and `convert_base`
            # are absent from the base list already.)
            "functions": {
                **CAPABILITIES["functions"],
                "string": [
                    fn for fn in CAPABILITIES["functions"]["string"]
                    if fn not in {
                        "reverse", "repeat", "lpad", "rpad", "translate",
                        "levenshtein", "as_date", "as_time", "as_timestamp",
                    }
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
                "quantile": False,
                "asofJoin": False,
                "samplingSeed": False,
                "unnest": False,
            },
        }
    caps = {**CAPABILITIES, "backend": f"{source.dialect} (mock)"}
    if source.kind == "demo":
        caps = CAPABILITIES
    elif source.kind == "d1":
        caps = {
            **CAPABILITIES,
            "backend": "sqlite (D1 snapshot)",
            "supports": {**CAPABILITIES["supports"], "savepoints": True},
        }
    elif source.kind == "postgres-live":
        regex = source.con.con.info.server_version >= 150000
        caps = {
            **CAPABILITIES,
            "backend": "postgres (live)",
            "supports": {
                **CAPABILITIES["supports"],
                # Ibis 12 renders regex matching with regexp_like (PostgreSQL 15+).
                "regex": regex,
                "savepoints": True,
            },
            "operators": [
                op for op in CAPABILITIES["operators"]
                if regex or op not in {"~", "!~"}
            ],
        }
    if source.con is None:
        return caps
    probed = _probes_for(source.con)
    dead = probed["dead"]
    string_fns = [
        fn for fn in caps["functions"]["string"] if fn not in dead
    ] + [
        fn for fn in URL_ACCESSORS if fn not in dead
        and fn not in caps["functions"]["string"]
    ]
    flags = {k: v for k, v in probed.items() if k != "dead"}
    return {
        **caps,
        "functions": {**caps["functions"], "string": string_fns},
        "supports": {**caps["supports"], **flags},
    }

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
    kind: str  # "demo" | "mock" | "d1" | "d1-live" | "postgres-live"
    dialect: str  # suggested default compile dialect
    dir: Path
    con: object = field(default=None, repr=False)
    datasets: dict[str, dict] = field(default_factory=dict)
    # A live Cloudflare D1 connection (d8r.engine.d1api.CloudflareD1) when
    # kind == "d1-live". Its presence tells the caller to ship compiled SQLite
    # SQL over the D1 HTTP API instead of executing on a local `con`.
    d1: object = None
    # Connection identity only: the password stays with the driver/profile owner.
    postgres: dict[str, str] = field(default_factory=dict, repr=False)
    schema_indexed: bool = True


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
#     API with the user's own credentials. Referenced table schemas are loaded
#     on demand into unbound ibis tables to compile payloads to SQLite SQL;
#     the SQL is then run back through the API (`d8r.engine.execute`).
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

    The initial request authenticates without discovering any tables. Referenced
    table schemas load on demand through the connection; the app can independently
    index the full schema for its explorer. A bad token or unknown database raises
    `D1Error` before anything registers.
    """
    d1 = client or CloudflareD1(account_id=account_id, api_token=api_token, database=database)
    try:
        d1.resolve()
        d1.check_connection()
        label = (display or "").strip() or f"D1 · {database}"
        return DataSource(
            id=source_id,
            display=label,
            doc=f"Cloudflare D1 · {d1.database_uuid}",
            kind="d1-live",
            dialect="sqlite",
            dir=Path(database),
            con=schema_connection({}, d1=d1),
            d1=d1,
            schema_indexed=False,
        )
    except Exception:
        d1.close()
        raise


def add_postgres_source(
    source_id: str,
    *,
    host: str,
    port: int | str = 5432,
    database: str,
    user: str,
    password: str,
    schema: str = "public",
    sslmode: str = "prefer",
    display: str | None = None,
) -> DataSource:
    """Connect explicitly to PostgreSQL and discover one schema without scanning rows.

    Ibis owns all query compilation/execution. Supplying an existing autocommit
    psycopg connection avoids retaining its password in Ibis connection kwargs;
    Ibis 12's ``from_connection`` only configures adapters and the UTC timezone,
    never creates extensions. Its query transaction contexts become savepoints
    inside an explicit ``BEGIN``, leaving the session's transaction in charge.
    """
    metadata = {}
    for key, value in (("host", host), ("database", database), ("user", user), ("schema", schema)):
        if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError(f"a valid PostgreSQL {key} is required")
        metadata[key] = value.strip()
    if "://" in metadata["host"] or "," in metadata["host"]:
        raise ValueError("PostgreSQL host must be one hostname or address, not a connection URL")
    if isinstance(port, bool) or not isinstance(port, (int, str)):
        raise ValueError("PostgreSQL port must be an integer from 1 to 65535")
    try:
        port_number = int(port)
    except ValueError:
        raise ValueError("PostgreSQL port must be an integer from 1 to 65535") from None
    if not 1 <= port_number <= 65535:
        raise ValueError("PostgreSQL port must be an integer from 1 to 65535")
    if sslmode not in {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}:
        raise ValueError("select a supported PostgreSQL SSL mode")
    if metadata["schema"].lower().startswith("pg_") or metadata["schema"].lower() == "information_schema":
        raise ValueError("select a PostgreSQL user schema, not a system schema")
    if not isinstance(password, str) or "\x00" in password:
        raise ValueError("a valid PostgreSQL password is required")
    metadata.update(port=str(port_number), sslmode=sslmode)

    raw = None
    stage = "connect"
    try:
        import psycopg

        raw = psycopg.connect(
            host=metadata["host"],
            port=port_number,
            dbname=metadata["database"],
            user=metadata["user"],
            password=password,
            sslmode=sslmode,
            connect_timeout=10,
            autocommit=True,
        )
        stage = "inspect"
        con = ibis.postgres.from_connection(raw)
        # Connection state, not query SQL. Identifier quoting makes commas,
        # quotes and dots a single schema name, never a search_path expression.
        with raw.cursor() as cursor:
            cursor.execute(
                psycopg.sql.SQL("SET search_path TO {}").format(
                    psycopg.sql.Identifier(metadata["schema"])
                )
            )
        if metadata["schema"] not in con.list_databases():
            raise ValueError("selected schema is unavailable")

        def public_text(value: str) -> str:
            return value.replace(password, "[redacted]") if password else value

        source = DataSource(
            id=source_id,
            display=public_text((display or "").strip() or f"PostgreSQL · {metadata['database']}"),
            doc=public_text(f"PostgreSQL · {metadata['database']} · {metadata['schema']}"),
            kind="postgres-live",
            dialect="postgres",
            dir=Path("."),
            con=con,
            postgres=metadata,
        )
        location = (metadata["database"], metadata["schema"])
        # Ibis 12 list_tables(public) also unions temporary names, even when
        # a schema was requested. Discover exactly this schema through Ibis
        # instead, without peeking at rows or other sessions' temp objects.
        relations = ops.DatabaseTable(
            "tables",
            ibis.schema({
                "table_catalog": "string",
                "table_schema": "string",
                "table_name": "string",
                "table_type": "string",
            }),
            source=con,
            namespace=ops.Namespace(database="information_schema"),
        ).to_expr()
        names = relations.filter(
            relations.table_catalog == metadata["database"],
            relations.table_schema == metadata["schema"],
            relations.table_type.isin(["BASE TABLE", "VIEW", "FOREIGN"]),
        ).select("table_name").order_by("table_name").execute()
        for name in names.table_name:
            source.datasets[name] = {
                "table": con.table(name, database=location),
                "doc": public_text(f"{name} · {metadata['schema']}"),
                "rows": None,
            }
        return source
    except Exception:
        if raw is not None:
            try:
                raw.close()
            except Exception:
                pass
        # Driver/server exceptions can contain a DSN, password or arbitrary
        # server text. Neither their message nor their chain reaches the UI.
        if stage == "connect":
            raise PayloadError(
                "PostgreSQL connection failed; check the host, port, database, credentials and SSL mode"
            ) from None
        raise PayloadError(
            "PostgreSQL schema discovery failed; check the schema, permissions and supported column types"
        ) from None
