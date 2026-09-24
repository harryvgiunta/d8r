"""The TUI's headless core: registry, schema seam, palette data, runs, history.

Everything the widgets need to know is computed here in plain Python, so the
Textual layer stays a thin shell and the tests can drive a `Session` without a
terminal attached. Nothing here opens a server or a socket: the only network
edge is the engine's own live-D1 path, reached exactly as the engine exposes it.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from anyql.engine import (
    D1Error,
    DIALECT_BY_NAME,
    DataSource,
    PayloadError,
    add_d1_live_source,
    add_sqlite_source,
    build,
    capabilities_for,
    column_values,
    compile_sql,
    dialect_for,
    execute,
    execute_remote,
    load,
    type_name,
)
from anyql.query import (
    Capabilities,
    ColumnDef,
    TableDef,
    column_by_name,
    column_pool,
    open_tables_of,
    parse_query,
    payload_from_ast,
    resolve_column,
    set_schema_state,
    table_by_name,
)

PREVIEW_ROW_CAP = 10000
"""Rows the results DataTable buffers; the rest of the result is left unread.

The engine still runs the document as written — this is a *preview* cap on what
the explorer holds, not a query rewrite, so the row count it reports stays true
to what the document asked for.
"""

VALUE_POOL_LIMIT = 1000
"""Distinct values fetched per column for the `\\where` value search.

The palette shows `palette.VALUE_SUGGESTIONS` of them at a time; the pool is
what the typing searches, so a value that sorts past the first page is still
found by typing part of it. One fetch per (source, dataset, column), cached.
"""


# ---------------------------------------------------------------------------
# Source -> schema seam
# ---------------------------------------------------------------------------


def tables_of(source: DataSource) -> list[TableDef]:
    """The source's datasets as registry tables (live ibis schemas, in order)."""
    tables: list[TableDef] = []
    for name, entry in source.datasets.items():
        schema = entry["table"].schema()
        columns = [ColumnDef(col, type_name(dtype)) for col, dtype in schema.items()]
        tables.append(TableDef(name=name, doc=entry.get("doc", ""), columns=columns))
    return tables


def capabilities_object(source: DataSource) -> Capabilities:
    """`capabilities_for(source)` as the registry's `Capabilities` record."""
    caps = capabilities_for(source)
    return Capabilities(
        backend=caps["backend"],
        aggregates=list(caps["aggregates"]),
        functions={family: list(fns) for family, fns in caps["functions"].items()},
        operators=list(caps["operators"]),
        window_functions=list(caps.get("windowFunctions", [])),
        supports=dict(caps.get("supports", {})),
    )


def default_dialect(source: DataSource) -> str:
    """The compile dialect a source suggests — or duckdb when it cannot render."""
    spec = DIALECT_BY_NAME.get(source.dialect)
    return source.dialect if spec is not None and spec["compiles"] else "duckdb"


def _source_id(hint: str, taken) -> str:
    """A stable, unique registry id derived from a human hint."""
    base = re.sub(r"[^a-z0-9]+", "-", hint.strip().lower()).strip("-") or "source"
    candidate = base
    suffix = 2
    while candidate in taken:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def looks_numeric(value: str) -> bool:
    """True for a value `\\where` can carry bare; text gets quoted instead."""
    try:
        float(value)
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------------------
# Results of one run
# ---------------------------------------------------------------------------


@dataclass
class HistoryEntry:
    """One executed document, as the History tab lists it."""

    at: str
    source: str
    dialect: str
    rows: int
    ms: float
    doc: str

    @property
    def title(self) -> str:
        """The document's first non-empty line, for the History table."""
        for line in self.doc.split("\n"):
            if line.strip():
                return line.strip()
        return ""


@dataclass
class RunOutcome:
    """What one `Run` produced: a rendered result, or just a message.

    `error` is the Results-tab line (empty when the run succeeded); `status` is
    always what the footer shows. `rows` is already capped at `PREVIEW_ROW_CAP`
    while `total` is what the document asked for.
    """

    status: str = ""
    error: str = ""
    ok: bool = False
    source: str = ""
    dialect: str = ""
    columns: list[str] = field(default_factory=list)
    dtypes: list[str] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    total: int = 0
    ms: float = 0.0
    sql: str = ""

    @property
    def capped(self) -> bool:
        """True when the preview buffer, not the document, decided the row count."""
        return self.total > len(self.rows)


# ---------------------------------------------------------------------------
# The session
# ---------------------------------------------------------------------------


class Session:
    """The app's state: which source is live, what dialect, what has run.

    Constructing one loads the bundled engine registry and installs its schema
    into the language layer's registry seam, so a session is always ready to
    parse a document against real columns — no separate "load" step.
    """

    def __init__(self, sources: dict[str, DataSource] | None = None) -> None:
        self.sources: dict[str, DataSource] = load() if sources is None else dict(sources)
        self.active_id: str = next(iter(self.sources))
        self.dialect: str = default_dialect(self.source)
        # The palette's master switch, owned by the Settings menu. In memory
        # like everything else here — it dies with the process.
        self.intellisense: bool = True
        self.history: list[HistoryEntry] = []
        self._values: dict[tuple[str, str, str], list[str]] = {}
        self.refresh_schema()

    # -- the active source --------------------------------------------------

    @property
    def source(self) -> DataSource:
        """The active datasource."""
        return self.sources[self.active_id]

    def set_active(self, source_id: str) -> None:
        """Point the session at another registered source and re-seam the schema."""
        if source_id not in self.sources:
            raise KeyError(source_id)
        self.active_id = source_id
        self.dialect = default_dialect(self.source)
        self.refresh_schema()

    def register(self, source: DataSource, activate: bool = True) -> None:
        """Add a source built by `add_sqlite_source`/`add_d1_live_source`."""
        self.sources[source.id] = source
        if activate:
            self.set_active(source.id)

    def refresh_schema(self) -> None:
        """Install the active source's tables + capabilities into the registry.

        This is the seam the parser's strict regime and the palette both read;
        it is refreshed on every source change, never cached across sources.
        """
        set_schema_state(tables_of(self.source), capabilities_object(self.source))

    # -- palette data -------------------------------------------------------

    def open_tables(self, doc: str):
        """The document's open tables (empty when it has no `\\from` yet)."""
        ast = parse_query(doc)
        if ast.from_ is None:
            return []
        return open_tables_of(ast.from_, ast.joins)

    def column_entries(self, doc: str) -> list[tuple[str, str, str]]:
        """(column, dtype, detail) rows for the palette, open tables first.

        With a `\\from` set the rows are exactly the document's open tables, in
        open order; without one they are the registry's cross-dataset pool.
        This is the data — which rows match what is typed is the palette's rule.
        """
        open_tables = self.open_tables(doc)
        rows: list[tuple[str, str, str]] = []
        if open_tables:
            for table_name in dict.fromkeys(t.dataset for t in open_tables):
                table = table_by_name(table_name)
                if table is None:
                    continue
                for column in table.columns:
                    rows.append((column.name, column.type, f"{column.type} · {table_name}"))
        else:
            rows = [
                (col.name, col.type, f"{col.type} · {', '.join(col.tables)}") for col in column_pool()
            ]
        return rows

    def dataset_entries(self) -> list[tuple[str, str]]:
        """(dataset, detail) rows for the palette's `\\from`/`\\open`/`\\join`."""
        return [(name, f"{entry['rows']} rows") for name, entry in self.source.datasets.items()]

    def values_for(self, doc: str, column: str) -> list[str]:
        """A column's distinct values, for the `\\where` value search.

        Cached per (source, dataset, column) and fetched up to
        `VALUE_POOL_LIMIT`, so typing can search past the first sorted page. A
        value fetch is best-effort, so a failure (a live D1 hiccup, a column with
        no value source) means no suggestions rather than an error to dismiss.
        """
        dataset = self.dataset_of(doc, column)
        if dataset is None:
            return []
        key = (self.active_id, dataset, column)
        if key not in self._values:
            try:
                self._values[key] = column_values(
                    self.source, dataset, column, limit=VALUE_POOL_LIMIT
                )
            except (PayloadError, D1Error, OSError, ValueError, KeyError):
                self._values[key] = []
        return self._values[key]

    def dataset_of(self, doc: str, column: str) -> str | None:
        """The dataset a column reference resolves to, if any."""
        resolved = resolve_column(column, self.open_tables(doc))
        if resolved is not None and resolved.tables:
            return resolved.tables[0]
        pooled = column_by_name(column)
        if pooled is not None and pooled.tables:
            return pooled.tables[0]
        return None

    # -- building sources (the add-source modal's engine edge) --------------

    def next_source_id(self, hint: str) -> str:
        """A registry id for a new source, kept unique against the live registry."""
        return _source_id(hint, self.sources)

    def build_sqlite_source(self, path: str, display: str = "") -> DataSource:
        """Build (not register) a local D1/SQLite snapshot source."""
        hint = display or Path(path).stem
        return add_sqlite_source(self.next_source_id(hint or "snapshot"), path, display or None)

    def build_live_source(
        self,
        account_id: str,
        api_token: str,
        database: str,
        display: str = "",
    ) -> DataSource:
        """Build (not register) a live Cloudflare D1 source over its HTTPS API."""
        hint = display or database or account_id
        return add_d1_live_source(
            self.next_source_id(hint),
            account_id=account_id,
            api_token=api_token,
            database=database,
            display=display or None,
        )

    # -- running ------------------------------------------------------------

    def run(self, doc: str) -> RunOutcome:
        """Parse, execute, and cap the result; never raises for a user mistake."""
        ast = parse_query(doc)
        if ast.errors:
            first = ast.errors[0]
            message = f"line {first.line}: {first.message}"
            return RunOutcome(status=f"not executed · {message}", error=message)
        payload = payload_from_ast(ast)
        try:
            dialect = dialect_for(self.source, self.dialect)
            expr = build(self.source.con, payload)
            if self.source.d1 is not None:
                result = execute_remote(self.source.d1, self.source.con, payload, dialect)
            else:
                result = execute(self.source.con, payload, dialect)
        except (PayloadError, D1Error) as exc:
            return RunOutcome(status=str(exc), error=str(exc))
        except Exception as exc:  # last-resort guard: a UI may not crash or exit
            message = f"{type(exc).__name__}: {exc}"
            return RunOutcome(status=message, error=message)

        returned = result["rows"]
        shown = returned[:PREVIEW_ROW_CAP]
        total = len(returned)
        if payload.get("limit") is None and len(shown) < total:
            counted = self.count_rows(expr)
            if counted is not None and counted > total:
                total = counted
        dtypes = self.dtypes_of(expr, len(result["columns"]))
        dialect = result["dialect"] or self.dialect
        outcome = RunOutcome(
            ok=True,
            source=self.active_id,
            dialect=dialect,
            columns=list(result["columns"]),
            dtypes=dtypes,
            rows=shown,
            total=total,
            ms=result["ms"],
            sql=result["sql"],
        )
        outcome.status = self._status_for(outcome)
        self.history.insert(
            0,
            HistoryEntry(
                at=time.strftime("%H:%M:%S"),
                source=self.active_id,
                dialect=dialect,
                rows=total,
                ms=result["ms"],
                doc=doc,
            ),
        )
        return outcome

    def compile(self, doc: str) -> tuple[str | None, str]:
        """(sql, message): the document rendered for the active dialect.

        Compiling never executes; `sql` is None when the document or the dialect
        is refused, and `message` is then the user-facing reason.
        """
        ast = parse_query(doc)
        if ast.errors:
            first = ast.errors[0]
            return None, f"line {first.line}: {first.message}"
        payload = payload_from_ast(ast)
        try:
            dialect = dialect_for(self.source, self.dialect)
            sql = compile_sql(build(self.source.con, payload), dialect)
        except (PayloadError, D1Error) as exc:
            return None, str(exc)
        except Exception as exc:  # last-resort guard: a UI may not crash or exit
            return None, f"{type(exc).__name__}: {exc}"
        return sql, f"compiled for {dialect} · {len(sql.splitlines())} lines"

    def count_rows(self, expr) -> int | None:
        """The document's full row count, for the preview-cap note.

        Runs the engine's own `count()` over the built expression; on a source
        that cannot answer locally (a live D1's unbound tables) it reports
        nothing and the caller falls back to what it actually read.
        """
        try:
            counted = expr.count().execute()
        except Exception:
            return None
        try:
            # An ungrouped count comes back as a scalar; a grouped one as a
            # frame with one count per group, whose sum is the row count.
            if hasattr(counted, "iloc"):
                return int(sum(int(value) for value in counted.iloc[:, 0] if value is not None))
            return int(counted)
        except (TypeError, ValueError, IndexError, AttributeError):
            return None

    @staticmethod
    def dtypes_of(expr, width: int) -> list[str]:
        """The result's dtype labels, aligned with the executed column list."""
        try:
            labels = [type_name(dtype) for _, dtype in expr.schema().items()]
        except Exception:
            labels = []
        if len(labels) != width:
            return [""] * width
        return labels

    @staticmethod
    def _status_for(outcome: RunOutcome) -> str:
        status = f"{len(outcome.rows)} rows · {outcome.ms:.1f} ms · {outcome.source} · {outcome.dialect}"
        if outcome.capped:
            status += f" · showing {len(outcome.rows)} of {outcome.total} (preview capped)"
        return status
