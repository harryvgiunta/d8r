"""The anyQL query AST — ported from an earlier TypeScript implementation.

The Python side spells fields in snake_case; `to_json()` emits the documented
wire shape (exactly the keys `spec/canonical-query.ast.json` carries), so the
port stays JSON-identical to that earlier contract. See `docs/AST.md`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ClauseKind = Literal[
    "with",
    "from",
    "join",
    "union",
    "intersect",
    "except",
    "select",
    "distinct",
    "where",
    "group",
    "order",
    "case",
    "temp",
    "drop",
    "begin",
    "commit",
    "rollback",
    "savepoint",
    "release",
]

AggregateFn = Literal["sum", "avg", "count", "min", "max"]
TemporalFn = Literal["year", "month", "day", "quarter", "hour", "minute", "second"]
RankFn = Literal["rank", "dense_rank", "row_number"]
RegexFn = Literal["regexp_extract", "regexp_replace"]
SetOpKind = Literal["union", "intersect", "except"]
Direction = Literal["asc", "desc"]
FrameKind = Literal["rows", "range"]
# A frame bound in its canonical text form: `unbounded preceding`, `<n> preceding`,
# `current row`, `<n> following`, `unbounded following`.
FrameBound = str
TxKind = Literal["begin", "commit", "rollback", "rollback_to", "savepoint", "release"]


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
class FrameBounds:
    """A window frame's `rows|range between <start> and <end>` bounds.

    Bounds are kept in their canonical text form (`unbounded preceding`, `2
    preceding`, `current row`, `3 following`, `unbounded following`) — the shape
    a user reads back — and mapped to engine offsets by the payload's consumer.
    """

    kind: FrameKind
    start: FrameBound
    end: FrameBound

    def to_json(self) -> dict:
        return {"kind": self.kind, "start": self.start, "end": self.end}


@dataclass
class WindowFrame:
    """The `over ( ... )` frame attached to a windowed select item."""

    partition_by: list[str] = field(default_factory=list)
    # Single ordering column inside `over (...)`, `None` when absent.
    order: WindowOrder | None = None
    # `rows|range between ... and ...`, `None` for a whole-partition frame.
    frame: FrameBounds | None = None

    def to_json(self) -> dict:
        return {
            "partitionBy": list(self.partition_by),
            "order": self.order.to_json() if self.order else None,
            "frame": self.frame.to_json() if self.frame else None,
        }


@dataclass
class RegexCall:
    """A regex projection: `regexp_extract(<arg>, <pattern>[, <group>])` or
    `regexp_replace(<arg>, <pattern>, <replacement>)` — both over a string column."""

    fn: RegexFn
    arg: str
    pattern: str
    # Capture group `regexp_extract` returns; 0 (the whole match) when absent.
    group: int | None = None
    replacement: str | None = None  # `regexp_replace`'s replacement text.

    def to_json(self) -> dict:
        return {
            "fn": self.fn,
            "arg": self.arg,
            "pattern": self.pattern,
            "group": self.group,
            "replacement": self.replacement,
        }


@dataclass
class LiteralValue:
    """A constant expression; the wrapper distinguishes NULL from no literal."""

    value: str | int | float | bool | None

    def to_json(self) -> dict:
        return {"value": self.value}


@dataclass
class ColumnRef:
    column: str

    def to_json(self) -> dict:
        return {"column": self.column}


@dataclass
class ScalarCall:
    """A catalog function whose arguments are columns, constants, or scalar calls."""

    fn: str
    args: list[ColumnRef | LiteralValue | ScalarCall]

    def to_json(self) -> dict:
        return {
            "fn": self.fn,
            "args": [
                {"literal": arg.to_json()} if isinstance(arg, LiteralValue) else arg.to_json()
                for arg in self.args
            ],
        }


@dataclass
class SelectItem:
    line: int  # 1-based document line the command lives on.
    raw: str  # Raw argument text as typed, e.g. `sum(amount) as revenue`.
    column: str | None = None  # Set when the item is a plain column reference.
    literal: LiteralValue | None = None
    scalar: ScalarCall | None = None
    # `\select *` — expands to every column of the open tables (leftmost-first dedup).
    star: bool = False
    aggregate: AggCall | None = None  # Aggregate call (windowless unless `window` is set).
    temporal: TemporalCall | None = None  # Temporal part extraction (a derived grouping key).
    rank: RankCall | None = None  # Rank call (`rank()` over (…)); takes no argument.
    window: WindowFrame | None = None  # `over ( ... )` frame; set exactly on windowed items.
    regex: RegexCall | None = None  # Regex extraction/replacement over a string column.
    # An inline `( \\commands )` subquery used as a scalar value (needs an alias).
    subquery: QueryAST | None = None
    alias: str | None = None

    def to_json(self) -> dict:
        return {
            "line": self.line,
            "raw": self.raw,
            "column": self.column,
            "literal": self.literal.to_json() if self.literal is not None else None,
            "scalar": self.scalar.to_json() if self.scalar else None,
            "star": self.star,
            "aggregate": self.aggregate.to_json() if self.aggregate else None,
            "temporal": self.temporal.to_json() if self.temporal else None,
            "rank": self.rank.to_json() if self.rank else None,
            "window": self.window.to_json() if self.window else None,
            "regex": self.regex.to_json() if self.regex else None,
            "subquery": self.subquery.to_json() if self.subquery else None,
            "alias": self.alias,
        }


@dataclass
class WhereClause:
    line: int
    raw: str
    column: str
    op: str
    value: str  # Value with surrounding quotes stripped.
    # Inline `( \\commands )` operand: `in`/`not in` take its single column, a
    # comparison takes it as a scalar subquery.
    subquery: QueryAST | None = None

    def to_json(self) -> dict:
        return {
            "line": self.line,
            "raw": self.raw,
            "column": self.column,
            "op": self.op,
            "value": self.value,
            "subquery": self.subquery.to_json() if self.subquery else None,
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
    # Dataset name; empty when the source is an inline `( … )` subquery, which
    # needs an alias — its body is then the source table.
    table: str
    # Table alias (`\from events as e` / `\from events e`); `None` when unaliased.
    alias: str | None = None
    body: QueryAST | None = None

    def to_json(self) -> dict:
        return {
            "line": self.line,
            "table": self.table,
            "alias": self.alias,
            "body": self.body.to_json() if self.body else None,
        }


@dataclass
class JoinClause:
    """One `\\join <dataset> [as] <alias> on <left>[ = <right>]` clause (always INNER).

    Two source shapes ride along: an inline `( … )` subquery (`body`, with the
    dataset name empty), and `lateral`, where the subquery may reference the
    tables accumulated to its left — SQL's `JOIN LATERAL` / `CROSS APPLY`.
    """

    line: int  # 1-based document line the command lives on.
    # Dataset name; empty when the source is the inline `( … )` subquery.
    dataset: str
    # Table alias (`\join users as u` / `\join users u`); `None` when unaliased.
    alias: str | None = None
    # Column on the accumulated left side (defaults to `right` when omitted).
    # May be identifier-qualified.
    left: str = ""
    right: str = ""  # Column on `dataset`.
    # `\join lateral` — the body may read the left side; `on` is optional then.
    lateral: bool = False
    body: QueryAST | None = None

    def to_json(self) -> dict:
        return {
            "line": self.line,
            "dataset": self.dataset,
            "alias": self.alias,
            "left": self.left,
            "right": self.right,
            "lateral": self.lateral,
            "body": self.body.to_json() if self.body else None,
        }


@dataclass
class SetOpClause:
    """One `\\union` / `\\intersect` / `\\except` clause (SQL's deduplicating default).

    The operation runs against the accumulated left query, in document order,
    exactly like a `\\join` runs against the accumulated left table. The operand
    is a dataset name or an inline `( … )` subquery (`body`).
    """

    line: int  # 1-based document line the command lives on.
    op: SetOpKind  # union keeps both sides, intersect only shared rows, except subtracts.
    # Dataset or CTE the operation runs against (`\\union events`, `\\union recent`);
    # empty when the operand is an inline subquery.
    dataset: str
    # SQL's default: rows are deduplicated. `\\union all` keeps duplicates.
    distinct: bool = True
    body: QueryAST | None = None

    def to_json(self) -> dict:
        return {
            "line": self.line,
            "op": self.op,
            "dataset": self.dataset,
            "distinct": self.distinct,
            "body": self.body.to_json() if self.body else None,
        }


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
class TempClause:
    """`\\temp <name>` — materialize this document's query as a temp table.

    The table lives on the active source's connection for as long as the
    process does; the schema registry gains it, so a later document can name it
    in `\\from`/`\\join`. Nothing is persisted (AGENTS.md's in-memory rule).
    """

    line: int
    name: str

    def to_json(self) -> dict:
        return {"line": self.line, "name": self.name}


@dataclass
class DropClause:
    """`\\drop <name>` — drop a temp table this session created."""

    line: int
    name: str

    def to_json(self) -> dict:
        return {"line": self.line, "name": self.name}


@dataclass
class TxDirective:
    """One transaction command: `\\begin`, `\\commit`, `\\rollback [to <name>]`,
    `\\savepoint <name>`, `\\release <name>`.

    Directives are statements, not query clauses: they run in document order
    before the document's query, against the active source's own connection.
    `name` is the savepoint a `savepoint`/`release` targets, and the one
    `rollback_to` rewinds to.
    """

    line: int
    kind: TxKind
    name: str | None = None

    def to_json(self) -> dict:
        return {"line": self.line, "kind": self.kind, "name": self.name}


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
    # Row deduplication after projection/set operations, before ordering and limit.
    distinct: bool = False
    where: WhereClause | None = None
    group_by: list[GroupTerm] = field(default_factory=list)
    order_by: list[OrderTerm] = field(default_factory=list)
    # `\case` computed columns in document order (projected after the select list).
    cases: list[CaseClause] = field(default_factory=list)
    # `\temp <name>` — materialize the query as a temp table (a later `\temp` replaces).
    temp: TempClause | None = None
    # `\drop <name>` — drop a temp table (a later `\drop` replaces).
    drop: DropClause | None = None
    # Transaction commands in document order; they run before the query does.
    tx: list[TxDirective] = field(default_factory=list)
    limit: int | None = None  # Row cap from `\limit` (last one wins); `None` when unset.
    errors: list[QueryError] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "with": [c.to_json() for c in self.with_],
            "from": self.from_.to_json() if self.from_ else None,
            "joins": [j.to_json() for j in self.joins],
            "setOps": [s.to_json() for s in self.set_ops],
            "select": [s.to_json() for s in self.select],
            "distinct": self.distinct,
            "where": self.where.to_json() if self.where else None,
            "groupBy": [g.to_json() for g in self.group_by],
            "orderBy": [o.to_json() for o in self.order_by],
            "cases": [c.to_json() for c in self.cases],
            "temp": self.temp.to_json() if self.temp else None,
            "drop": self.drop.to_json() if self.drop else None,
            "tx": [t.to_json() for t in self.tx],
            "limit": self.limit,
            "errors": [e.to_json() for e in self.errors],
        }
