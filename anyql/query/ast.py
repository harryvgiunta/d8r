"""The anyQL query AST — ported from an earlier TypeScript implementation.

The Python side spells fields in snake_case; `to_json()` emits the documented
wire shape (exactly the keys `spec/canonical-query.ast.json` carries), so the
port stays JSON-identical to that earlier contract. See `docs/AST.md`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ClauseKind = Literal["with", "from", "join", "union", "intersect", "except", "select", "where", "group", "order", "case"]

AggregateFn = Literal["sum", "avg", "count", "min", "max"]
TemporalFn = Literal["year", "month", "day", "quarter", "hour", "minute", "second"]
RankFn = Literal["rank", "dense_rank", "row_number"]
SetOpKind = Literal["union", "intersect", "except"]
Direction = Literal["asc", "desc"]


@dataclass
class AggCall:
    fn: AggregateFn
    arg: str

    def to_json(self) -> dict:
        return {"fn": self.fn, "arg": self.arg}


@dataclass
class TemporalCall:
    """Temporal extraction: a dtype-aware part of a temporal column (`year(timestamp)`)."""

    fn: TemporalFn
    arg: str

    def to_json(self) -> dict:
        return {"fn": self.fn, "arg": self.arg}


@dataclass
class RankCall:
    fn: RankFn

    def to_json(self) -> dict:
        return {"fn": self.fn}


@dataclass
class WindowOrder:
    column: str
    direction: Direction

    def to_json(self) -> dict:
        return {"column": self.column, "direction": self.direction}


@dataclass
class WindowFrame:
    """The `over ( ... )` frame attached to a windowed select item."""

    partition_by: list[str] = field(default_factory=list)
    # Single ordering column inside `over (...)`, `None` when absent.
    order: WindowOrder | None = None

    def to_json(self) -> dict:
        return {
            "partitionBy": list(self.partition_by),
            "order": self.order.to_json() if self.order else None,
        }


@dataclass
class SelectItem:
    line: int  # 1-based document line the command lives on.
    raw: str  # Raw argument text as typed, e.g. `sum(amount) as revenue`.
    column: str | None = None  # Set when the item is a plain column reference.
    # `\select *` — expands to every column of the open tables (leftmost-first dedup).
    star: bool = False
    aggregate: AggCall | None = None  # Aggregate call (windowless unless `window` is set).
    temporal: TemporalCall | None = None  # Temporal part extraction (a derived grouping key).
    rank: RankCall | None = None  # Rank call (`rank()` over (…)); takes no argument.
    window: WindowFrame | None = None  # `over ( ... )` frame; set exactly on windowed items.
    alias: str | None = None

    def to_json(self) -> dict:
        return {
            "line": self.line,
            "raw": self.raw,
            "column": self.column,
            "star": self.star,
            "aggregate": self.aggregate.to_json() if self.aggregate else None,
            "temporal": self.temporal.to_json() if self.temporal else None,
            "rank": self.rank.to_json() if self.rank else None,
            "window": self.window.to_json() if self.window else None,
            "alias": self.alias,
        }


@dataclass
class WhereClause:
    line: int
    raw: str
    column: str
    op: str
    value: str  # Value with surrounding quotes stripped.

    def to_json(self) -> dict:
        return {
            "line": self.line,
            "raw": self.raw,
            "column": self.column,
            "op": self.op,
            "value": self.value,
        }


@dataclass
class GroupTerm:
    line: int
    column: str

    def to_json(self) -> dict:
        return {"line": self.line, "column": self.column}


@dataclass
class OrderTerm:
    line: int
    target: str
    direction: Direction
    # 1-based index into `select` when the target resolves to a select item.
    resolves_to: int | None = None

    def to_json(self) -> dict:
        return {
            "line": self.line,
            "target": self.target,
            "direction": self.direction,
            "resolvesTo": self.resolves_to,
        }


@dataclass
class FromClause:
    line: int
    table: str
    # Table alias (`\from events as e` / `\from events e`); `None` when unaliased.
    alias: str | None = None

    def to_json(self) -> dict:
        return {"line": self.line, "table": self.table, "alias": self.alias}


@dataclass
class JoinClause:
    """One `\\join <dataset> [as] <alias> on <left>[ = <right>]` clause (always INNER)."""

    line: int  # 1-based document line the command lives on.
    dataset: str
    # Table alias (`\join users as u` / `\join users u`); `None` when unaliased.
    alias: str | None = None
    # Column on the accumulated left side (defaults to `right` when omitted).
    # May be identifier-qualified.
    left: str = ""
    right: str = ""  # Column on `dataset`.

    def to_json(self) -> dict:
        return {
            "line": self.line,
            "dataset": self.dataset,
            "alias": self.alias,
            "left": self.left,
            "right": self.right,
        }


@dataclass
class SetOpClause:
    """One `\\union` / `\\intersect` / `\\except` clause (SQL's deduplicating default).

    The operation runs against the accumulated left query, in document order,
    exactly like a `\\join` runs against the accumulated left table.
    """

    line: int  # 1-based document line the command lives on.
    op: SetOpKind  # union keeps both sides, intersect only shared rows, except subtracts.
    # Dataset or CTE the operation runs against (`\\union events`, `\\union recent`).
    dataset: str
    # SQL's default: rows are deduplicated. `\\union all` keeps duplicates.
    distinct: bool = True

    def to_json(self) -> dict:
        return {"line": self.line, "op": self.op, "dataset": self.dataset, "distinct": self.distinct}


@dataclass
class QueryError:
    line: int
    message: str

    def to_json(self) -> dict:
        return {"line": self.line, "message": self.message}


@dataclass
class CaseBranch:
    """One `when <column> <op> <value> then <value>` branch of a `\\case` clause."""

    column: str  # Condition column (bare or identifier-qualified).
    op: str
    value: str  # Condition value with surrounding quotes stripped.
    then: str  # Branch result literal, quotes stripped.

    def to_json(self) -> dict:
        return {"column": self.column, "op": self.op, "value": self.value, "then": self.then}


@dataclass
class CaseClause:
    """A `\\case <alias> = when … then … [when … then …] [else …]` computed column."""

    line: int
    raw: str
    alias: str
    whens: list[CaseBranch] = field(default_factory=list)
    # Else literal (quotes stripped); `None` when absent (defaults to NULL).
    else_: str | None = None

    def to_json(self) -> dict:
        return {
            "line": self.line,
            "raw": self.raw,
            "alias": self.alias,
            "whens": [w.to_json() for w in self.whens],
            "else": self.else_,
        }


@dataclass
class WithClause:
    """A `\\with <name>` CTE block: the header line plus its indented body.

    The body is a full QueryAST with `errors` hoisted to the outer document and
    `with` always empty (nested CTEs are rejected). Bodies see every CTE defined
    BEFORE this one as a table.
    """

    line: int
    name: str
    body: QueryAST

    def to_json(self) -> dict:
        return {"line": self.line, "name": self.name, "body": self.body.to_json()}


@dataclass
class QueryAST:
    """The document's parsed shape (see `docs/AST.md`)."""

    # `\with` CTE blocks in document order; later bodies may reference earlier names.
    with_: list[WithClause] = field(default_factory=list)
    from_: FromClause | None = None
    # `\join` clauses in document order (applied left-deep onto `from`).
    joins: list[JoinClause] = field(default_factory=list)
    # `\union`/`\intersect`/`\except` clauses in document order (applied onto the
    # built left query, before `\order`/`\limit`).
    set_ops: list[SetOpClause] = field(default_factory=list)
    select: list[SelectItem] = field(default_factory=list)
    where: WhereClause | None = None
    group_by: list[GroupTerm] = field(default_factory=list)
    order_by: list[OrderTerm] = field(default_factory=list)
    # `\case` computed columns in document order (projected after the select list).
    cases: list[CaseClause] = field(default_factory=list)
    limit: int | None = None  # Row cap from `\limit` (last one wins); `None` when unset.
    errors: list[QueryError] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "with": [c.to_json() for c in self.with_],
            "from": self.from_.to_json() if self.from_ else None,
            "joins": [j.to_json() for j in self.joins],
            "setOps": [s.to_json() for s in self.set_ops],
            "select": [s.to_json() for s in self.select],
            "where": self.where.to_json() if self.where else None,
            "groupBy": [g.to_json() for g in self.group_by],
            "orderBy": [o.to_json() for o in self.order_by],
            "cases": [c.to_json() for c in self.cases],
            "limit": self.limit,
            "errors": [e.to_json() for e in self.errors],
        }
