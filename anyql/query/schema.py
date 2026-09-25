"""Immutable schema snapshots at the language layer's backend boundary.

Parsing without a snapshot uses ``EMPTY_SCHEMA`` and quietly permits unknown
columns and tables. Sessions replace their own snapshot when a source changes;
a parse retains its captured tables, capabilities, and saved functions.
The wire contract remains ``docs/AST.md`` and ``spec/canonical-query.ast.json``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Literal

from .functions import SCALAR_FUNCTIONS

AGGREGATES: tuple[str, ...] = ("sum", "avg", "count", "min", "max")

# Temporal extraction functions; the capability map selects by dtype family.
TEMPORAL: tuple[str, ...] = ("year", "month", "day", "quarter", "hour", "minute", "second")

DtypeFamily = Literal["timestamp", "date", "time", "string"]


@dataclass(frozen=True)
class ColumnDef:
    name: str
    # Backend dtype name (ibis `dtype.name()`): "int64", "float64", "string", ...
    type: str
    doc: str = ""
    # Declared enum-ish domain; used as the sync fallback for value completion.
    values: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.values is not None:
            object.__setattr__(self, "values", tuple(self.values))


@dataclass(frozen=True)
class TableDef:
    name: str
    doc: str = ""
    columns: tuple[ColumnDef, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "columns", tuple(self.columns))


@dataclass(frozen=True)
class Capabilities:
    """What the connected backend actually supports — the capability filter."""

    backend: str
    aggregates: tuple[str, ...]
    # dtype family -> scalar functions usable on that type (timestamp -> year, ...).
    functions: Mapping[str, tuple[str, ...]]
    operators: tuple[str, ...]
    # Rank-style window functions usable as `<fn>() over ( … )` in `\\select`.
    window_functions: tuple[str, ...] = ()
    supports: Mapping[str, bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "aggregates", tuple(self.aggregates))
        object.__setattr__(self, "functions", MappingProxyType({
            family: tuple(functions) for family, functions in self.functions.items()
        }))
        object.__setattr__(self, "operators", tuple(self.operators))
        object.__setattr__(self, "window_functions", tuple(self.window_functions))
        object.__setattr__(self, "supports", MappingProxyType(dict(self.supports)))


def dtype_family(type: str) -> DtypeFamily | None:
    """The dtype families recognized by scalar function completion."""
    if type == "timestamp":
        return "timestamp"
    if type == "date":
        return "date"
    if type == "time":
        return "time"
    if type == "string":
        return "string"
    return None


# Conservative duckdb defaults per the sidecar contract.
DEFAULT_CAPABILITIES = Capabilities(
    backend="duckdb",
    aggregates=AGGREGATES,
    functions={
        "timestamp": TEMPORAL,
        "date": ("year", "month", "day", "quarter"),
        "time": ("hour", "minute", "second"),
        "string": tuple(fn for fn in SCALAR_FUNCTIONS if fn != "string"),
        "any": ("string",),
    },
    operators=("=", "!=", ">", ">=", "<", "<=", "like"),
    window_functions=("rank", "dense_rank", "row_number"),
    supports={"groupBy": True, "orderBy": True, "limit": True, "distinct": True, "like": True},
)


@dataclass(frozen=True)
class PoolColumn(ColumnDef):
    """Flat, name-deduplicated pool of every known dataset field."""

    # Datasets defining this column, in load order.
    tables: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "tables", tuple(self.tables))


@dataclass(frozen=True)
class FnDef:
    """A saved table-valued function: a named, parameterized query body.

    The body is a document's worth of `\\command` text whose parameters are
    written `@name`; calling the function substitutes the arguments for those
    tokens and parses the result as a relation body, so the AST, payload, and
    engine never learn a function exists. `params` is the positional signature
    (names in argument order); `doc` is the one-line description the library
    and the palette show.
    """

    name: str
    params: tuple[str, ...] = ()
    body: str = ""
    doc: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "params", tuple(self.params))


@dataclass(frozen=True)
class OpenTable:
    """One of the query's open tables: its identifier is the alias when set."""

    dataset: str
    identifier: str


@dataclass(frozen=True)
class SchemaContext:
    """One coherent, deeply immutable schema captured for a parse or completion."""

    tables: tuple[TableDef, ...] = ()
    capabilities: Capabilities = DEFAULT_CAPABILITIES
    fns: tuple[FnDef, ...] = ()
    pool: tuple[PoolColumn, ...] = field(init=False)
    _fns_by_name: Mapping[str, FnDef] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "tables", tuple(self.tables))
        object.__setattr__(self, "fns", tuple(self.fns))
        object.__setattr__(self, "_fns_by_name", MappingProxyType({fn.name: fn for fn in self.fns}))
        if not self.capabilities.window_functions:
            object.__setattr__(self, "capabilities", replace(
                self.capabilities, window_functions=DEFAULT_CAPABILITIES.window_functions,
            ))
        by_name: dict[str, tuple[ColumnDef, list[str]]] = {}
        for table in self.tables:
            for column in table.columns:
                existing = by_name.get(column.name)
                if existing is None:
                    by_name[column.name] = (column, [table.name])
                else:
                    existing[1].append(table.name)
        object.__setattr__(self, "pool", tuple(
            PoolColumn(column.name, column.type, column.doc, column.values, tuple(tables))
            for column, tables in by_name.values()
        ))

    def table_by_name(self, name: str) -> TableDef | None:
        return next((table for table in self.tables if table.name == name), None)

    def column_by_name(self, name: str) -> PoolColumn | None:
        return next((column for column in self.pool if column.name == name), None)

    def fn_by_name(self, name: str) -> FnDef | None:
        return self._fns_by_name.get(name)

    def resolve_qualified(
        self, prefix: str, name: str, open_tables: Sequence[OpenTable],
    ) -> tuple[ColumnDef | None, str | None]:
        """Resolve `identifier.column` with a strict alias/prefix match."""
        match = next((table for table in open_tables if table.identifier == prefix), None)
        table = self.table_by_name(match.dataset) if match else None
        if table is None:
            return None, None
        column = next((column for column in table.columns if column.name == name), None)
        return column, table.name

    def resolve_column(self, name: str, open_tables: Sequence[OpenTable]) -> PoolColumn | None:
        """Resolve a bare column left-to-right; the matched table leads `tables`."""
        for open_table in open_tables:
            table = self.table_by_name(open_table.dataset)
            if table is None:
                continue
            column = next((column for column in table.columns if column.name == name), None)
            if column is None:
                continue
            pooled = self.column_by_name(name)
            tables = pooled.tables if pooled else ()
            return PoolColumn(
                column.name, column.type, column.doc, column.values,
                (open_table.dataset, *(table for table in tables if table != open_table.dataset)),
            )
        return None


EMPTY_SCHEMA = SchemaContext()


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
