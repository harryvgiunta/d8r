"""Schema seam — ported from an earlier TypeScript implementation.

The single edge between the language layer and the live backend.
`set_schema_state(tables, capabilities)` is called by the data layer whenever
it (re)delivers schemas and capabilities; before the first load the registry is
simply empty, so the parser stays pure and degrades quietly instead of
complaining about columns that don't exist yet. The contract is `docs/AST.md`,
pinned by `spec/canonical-query.ast.json`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal

AGGREGATES: tuple[str, ...] = ("sum", "avg", "count", "min", "max")

# Temporal extraction functions; the capability map selects by dtype family.
TEMPORAL: tuple[str, ...] = ("year", "month", "day", "quarter", "hour", "minute", "second")

DtypeFamily = Literal["timestamp", "date", "time"]


@dataclass
class ColumnDef:
    name: str
    # Backend dtype name (ibis `dtype.name()`): "int64", "float64", "string", ...
    type: str
    doc: str = ""
    # Declared enum-ish domain; used as the sync fallback for value completion.
    values: list[str] | None = None


@dataclass
class TableDef:
    name: str
    doc: str = ""
    columns: list[ColumnDef] = field(default_factory=list)


@dataclass
class Capabilities:
    """What the connected backend actually supports — the capability filter."""

    backend: str
    aggregates: list[str]
    # dtype family -> scalar functions usable on that type (timestamp -> year, ...).
    functions: dict[str, list[str]]
    operators: list[str]
    # Rank-style window functions usable as `<fn>() over ( … )` in `\\select`.
    window_functions: list[str] = field(default_factory=list)
    supports: dict[str, bool] = field(default_factory=dict)


def dtype_family(type: str) -> DtypeFamily | None:
    """The dtype families recognized for temporal extraction."""
    if type == "timestamp":
        return "timestamp"
    if type == "date":
        return "date"
    if type == "time":
        return "time"
    return None


# Conservative duckdb defaults per the sidecar contract; replaced on load.
DEFAULT_CAPABILITIES = Capabilities(
    backend="duckdb",
    aggregates=["sum", "avg", "count", "min", "max"],
    functions={
        "timestamp": ["year", "month", "day", "quarter", "hour", "minute", "second"],
        "date": ["year", "month", "day", "quarter"],
        "time": ["hour", "minute", "second"],
    },
    operators=["=", "!=", ">", ">=", "<", "<=", "like"],
    window_functions=["rank", "dense_rank", "row_number"],
    supports={"groupBy": True, "orderBy": True, "limit": True, "like": True},
)


@dataclass
class PoolColumn(ColumnDef):
    """Flat, name-deduplicated pool of every known dataset field."""

    # Datasets defining this column, in load order.
    tables: list[str] = field(default_factory=list)


@dataclass
class OpenTable:
    """One of the query's open tables: its identifier is the alias when set."""

    dataset: str
    identifier: str


_tables: list[TableDef] = []
_capabilities_state: Capabilities = DEFAULT_CAPABILITIES
_pool: list[PoolColumn] = []


def set_schema_state(next_tables: list[TableDef], next_capabilities: Capabilities | None = None) -> None:
    """Install the live schema + capabilities (called on every refresh)."""
    global _tables, _capabilities_state, _pool
    _tables = list(next_tables)
    if next_capabilities is not None:
        _capabilities_state = replace(
            next_capabilities,
            window_functions=next_capabilities.window_functions or DEFAULT_CAPABILITIES.window_functions,
        )
    by_name: dict[str, PoolColumn] = {}
    for table in _tables:
        for col in table.columns:
            existing = by_name.get(col.name)
            if existing is not None:
                existing.tables.append(table.name)
            else:
                by_name[col.name] = PoolColumn(col.name, col.type, col.doc, col.values, [table.name])
    _pool = list(by_name.values())


def schema_tables() -> list[TableDef]:
    """All currently loaded tables, in load order."""
    return _tables


def column_pool() -> list[PoolColumn]:
    """The deduplicated cross-dataset column pool."""
    return _pool


def capabilities() -> Capabilities:
    """The active capability set (duckdb defaults until the backend answers)."""
    return _capabilities_state


def table_by_name(name: str) -> TableDef | None:
    for table in _tables:
        if table.name == name:
            return table
    return None


def column_by_name(name: str) -> PoolColumn | None:
    for col in _pool:
        if col.name == name:
            return col
    return None


def open_tables_of(from_clause, joins) -> list[OpenTable]:
    """The query's open tables in open order (`\\from`/`\\open`, then `\\join`s).

    Each open table's identifier is the alias when set, otherwise the dataset
    name, and is what qualified refs resolve against (aliasing is strict — an
    aliased table's dataset name no longer resolves).
    """
    out: list[OpenTable] = []
    if from_clause is not None:
        out.append(OpenTable(from_clause.table, from_clause.alias or from_clause.table))
    for join in joins:
        out.append(OpenTable(join.dataset, join.alias or join.dataset))
    return out


def resolve_qualified(prefix: str, name: str, open: list[OpenTable]) -> tuple[ColumnDef | None, str | None]:
    """Resolve a qualified `identifier.column` reference (STRICT prefix match)."""
    match = next((o for o in open if o.identifier == prefix), None)
    table = table_by_name(match.dataset) if match else None
    if table is None:
        return None, None
    col = next((c for c in table.columns if c.name == name), None)
    return col, table.name


def resolve_column(name: str, open_tables: list[OpenTable]) -> PoolColumn | None:
    """Resolve a bare column against open tables left-to-right: first table wins.

    `None` when no open table has it (or the schema is not loaded). The matched
    open table leads the returned `tables` list, so consumers read `tables[0]`
    as the table a bare reference resolves to.
    """
    for open_table in open_tables:
        table = table_by_name(open_table.dataset)
        if table is None:
            continue
        col = next((c for c in table.columns if c.name == name), None)
        if col is None:
            continue
        pool_entry = column_by_name(name)
        tables = list(pool_entry.tables) if pool_entry else []
        return PoolColumn(
            col.name,
            col.type,
            col.doc,
            col.values,
            [open_table.dataset, *[t for t in tables if t != open_table.dataset]],
        )
    return None
