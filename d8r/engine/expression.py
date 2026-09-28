"""Pure payload -> Ibis expression compilation for D8R's Python engine.

No UI concerns live here: user-facing mistakes raise `PayloadError`, which the
caller surfaces to the user as-is.
"""

from __future__ import annotations

import functools
import operator
import re
from collections.abc import Callable
from operator import eq, ge, gt, le, lt, ne

import ibis
import ibis.expr.types as ir
from ibis.common.exceptions import IbisError

from d8r.query.functions import (
    ARITHMETIC_PRECEDENCE,
    MAX_EXPRESSION_DEPTH,
    MAX_EXPRESSION_NODES,
    MAX_WHERE_DEPTH,
    MAX_WHERE_NODES,
    SCALAR_FUNCTIONS,
)

from .d1api import D1Error

__all__ = ["PayloadError", "AGGREGATE_FNS", "TEMPORAL_FNS", "OPERATORS", "col", "build", "compile_sql"]


class PayloadError(Exception):
    """User-level query error (unknown dataset/column/aggregation/op, bad limit)."""


AGGREGATE_FNS = frozenset({"sum", "avg", "count", "min", "max"})
COMPARISONS = {"=": eq, "!=": ne, ">": gt, ">=": ge, "<": lt, "<=": le}
# The regex operators: POSIX-style matching (`~`) and its negation (`!~`).
REGEX_OPS = frozenset({"~", "!~"})
# The operators whose operand is an inline subquery or a literal list.
SUBQUERY_OPS = frozenset({"in", "not in"})
# Operators with no operand at all.
NULL_OPS = frozenset({"is null", "is not null"})
# The range predicate: `between <low> and <high>`.
BETWEEN_OPS = frozenset({"between"})
OPERATORS = frozenset(COMPARISONS) | {
    "like", "ilike", *REGEX_OPS, *SUBQUERY_OPS, *NULL_OPS, *BETWEEN_OPS,
}

# Temporal extraction functions: dtype family -> method on the ibis column.
# A temporal call is a derived grouping column (`year(timestamp)` ->
# `EXTRACT(year ...)`), not an aggregate; the front end only offers these
# where the capabilities' `functions` map (mirroring this table) allows the
# column's dtype.
TEMPORAL_FNS: dict[str, frozenset[str]] = {
    "timestamp": frozenset({"year", "month", "day", "quarter", "hour", "minute", "second"}),
    "date": frozenset({"year", "month", "day", "quarter"}),
    "time": frozenset({"hour", "minute", "second"}),
}


def _dtype_family(dtype) -> str | None:
    if dtype.is_timestamp():
        return "timestamp"
    if dtype.is_date():
        return "date"
    if dtype.is_time():
        return "time"
    return None


def get_table(
    con,
    dataset: object,
    ctes: dict[str, ir.Table] | None = None,
    tables: dict[str, ir.Table] | None = None,
) -> ir.Table:
    """Resolve a dataset: a built CTE shadows a registered table of the same name.

    `tables` are the source's own datasets, already resolved to expressions by
    whoever owns the registry. They win over asking the connection, which is not
    only cheaper but *correct* on a backend that introspects when it is asked
    for a table by name: SQLite's `con.table()` reads the schema inside a
    transaction of its own, which would commit the session's.
    """
    if not isinstance(dataset, str) or not dataset:
        raise PayloadError("dataset must be a non-empty string")
    if ctes and dataset in ctes:
        return ctes[dataset]
    if tables and dataset in tables:
        return tables[dataset]
    try:
        return con.table(dataset)
    except D1Error:
        raise
    except Exception as exc:  # backend raises IbisError/KeyError for unknown names
        raise PayloadError(f"unknown dataset: {dataset}") from exc


def col(frames: list[tuple[str, ir.Table]], ref: object, what: str = "column") -> ir.Column:
    """Resolve a (possibly qualified `identifier.column`) reference against open tables.

    Frames are keyed by identifier: the table's alias when one was given, else its
    dataset name. Aliasing is strict — once aliased, only the alias resolves as a
    qualified prefix; the dataset name no longer matches. Bare names resolve to the
    leftmost frame containing them — open-table order is source (`dataset`) then
    joins in document order.
    """
    if isinstance(ref, str) and "." in ref:
        dataset, _, name = ref.partition(".")
        for frame_name, table in frames:
            if frame_name == dataset:
                if name in table.columns:
                    return table[name]
                break
        raise PayloadError(f"unknown {what}: {ref!r}")
    if isinstance(ref, str):
        for _, table in frames:
            if ref in table.columns:
                return table[ref]
    raise PayloadError(f"unknown {what}: {ref!r}")


def _build_source(
    con,
    dataset: object,
    body: object,
    ctes: dict[str, ir.Table] | None,
    tables: dict[str, ir.Table] | None,
) -> ir.Table:
    """A `\\from`/`\\join`/set-op source: a registered dataset or CTE, or an inline body.

    An inline `( … )` subquery is a query of its own and builds recursively
    against the same connection, with the same CTEs in scope — the body is a
    relation, never string SQL.
    """
    if body is not None:
        if not isinstance(body, dict):
            raise PayloadError("a subquery source must be an object")
        return build(con, body, ctes, tables)
    return get_table(con, dataset, ctes, tables)


def _outer_ref(ref: object, frames: list[tuple[str, ir.Table]]) -> bool:
    """True when a `\\where` operand names a column outside the lateral body."""
    if not isinstance(ref, str) or "." not in ref:
        return False
    prefix = ref[: ref.index(".")]
    return any(prefix == name for name, _ in frames)


_LATERAL_ROW = "d8r_lateral_row"


def _body_column(right: ir.Table, ref: object, what: str) -> ir.Column:
    """A column of a lateral body's own output.

    The body is one built relation, so its column resolves by name however it
    was qualified inside the body (`e.user_id` names the same column as
    `user_id` here) — the qualifier belongs to the body's own tables, not to the
    name the join gives that relation.
    """
    if not isinstance(ref, str):
        raise PayloadError(f"unknown {what}: {ref!r}")
    name = ref.rpartition(".")[2]
    if name in right.columns:
        return right[name]
    raise PayloadError(f"unknown {what}: {ref!r}")


def _tree_leaves(where: dict) -> tuple[list[dict], bool]:
    """Every naming condition of a where-tree, and whether it has a group.

    Descends `( ... )` groups (`group` subtrees) as well as `ands`/`ors`.
    A group node itself names no column — its head is empty — so it is not
    a leaf; only the conditions inside are. Iterative: nesting is bounded
    at parse time (`MAX_WHERE_DEPTH`)."""
    leaves: list[dict] = []
    grouped = False
    stack: list[object] = [where]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        inner = node.get("group")
        if inner is not None:
            grouped = True
            stack.append(inner)
        else:
            leaves.append(node)
        for cond in node.get("ands") or []:
            if isinstance(cond, dict):
                stack.append(cond)
        for grp in node.get("ors") or []:
            if isinstance(grp, list):
                stack.extend(item for item in grp if isinstance(item, dict))
            elif isinstance(grp, dict):
                stack.append(grp)
    return leaves, grouped


def _tree_correlates(where: dict, frames: list[tuple[str, ir.Table]]) -> bool:
    """True when any condition of a (possibly composed, possibly grouped)
    `\\where` reads an outer column — group contents included."""
    leaves, _ = _tree_leaves(where)
    return any(
        _outer_ref(leaf.get("column"), frames) or _outer_ref(leaf.get("value"), frames)
        for leaf in leaves
    )


def _hoist_correlation(
    where: dict, frames: list[tuple[str, ir.Table]]
) -> tuple[object, object, dict | None]:
    """Split a lateral body's `\\where` into its correlation and its remainder.

    An `and` chain may carry the correlating equality anywhere in the chain:
    that one leaf leaves the body and becomes the join's own equality; the
    rest stays the body's filter. An equality hoisted out of an `or` would
    change the body's meaning, so a body that correlates through `or` is
    refused outright, never approximated — and the same holds for a `( ... )`
    group, whose correlation ibis could not see through to the join.
    """
    if where.get("ors") and _tree_correlates(where, frames):
        raise PayloadError(
            "a lateral body correlates through `= <outer column>` joined by `and`, "
            "never through `or`"
        )
    _, grouped = _tree_leaves(where)
    leaves = [where, *(where.get("ands") or [])]
    found = next(
        (
            index
            for index, leaf in enumerate(leaves)
            if isinstance(leaf, dict)
            and (_outer_ref(leaf.get("column"), frames) or _outer_ref(leaf.get("value"), frames))
        ),
        None,
    )
    if found is None:
        if grouped and _tree_correlates(where, frames):
            raise PayloadError(
                "a lateral body cannot correlate through a `( ... )` group — "
                "write the `= <outer column>` equality ungrouped"
            )
        return None, None, where
    leaf = leaves[found]
    column, value, op = leaf.get("column"), leaf.get("value"), leaf.get("op")
    if op != "=" or _outer_ref(column, frames) == _outer_ref(value, frames):
        raise PayloadError(
            "a lateral body correlates through `= <outer column>` in its \\where: "
            f"{column} {op} {value}"
        )
    left_ref, right_ref = (column, value) if _outer_ref(column, frames) else (value, column)
    ands = [item for item in (where.get("ands") or []) if isinstance(item, dict)]
    if found == 0:
        if not ands:
            residual = None
        else:
            residual = dict(ands[0])
            if ands[1:]:
                residual["ands"] = ands[1:]
            else:
                residual.pop("ands", None)
    else:
        residual = dict(where)
        rest = [item for index, item in enumerate(ands) if index != found - 1]
        if rest:
            residual["ands"] = rest
        else:
            residual.pop("ands", None)
    if residual is not None and _tree_correlates(residual, frames):
        # A second outer-reading condition — inside a group or further along
        # the `and` chain — cannot ride the join; ibis would fail on it with
        # a column error. Name the mistake instead of crashing on it.
        raise PayloadError(
            "a lateral body correlates through one `= <outer column>` equality "
            "joined by `and`; the rest may not read an outer column"
        )
    return left_ref, right_ref, residual


def _lateral_parts(
    con,
    spec: dict,
    frames: list[tuple[str, ir.Table]],
    ctes: dict[str, ir.Table] | None,
    tables: dict[str, ir.Table] | None,
) -> tuple[ir.Table, object, list[ir.Column], list, int | None]:
    """A lateral join's right table, join predicate, partition keys and row cap.

    ibis builds no correlated subquery inside a join, so the correlation is
    *hoisted* instead: the body's `\\where` equality against an outer column
    leaves the body and becomes an equality in the join's own predicate — the
    same relation SQL's LATERAL produces when the correlation is an equality.
    A `\\limit` in the body then has to mean *per left row*, so the engine
    expresses it the way query planners rewrite it: a `row_number()` over the
    correlation's partition, filtered, with the helper column dropped again.

    Returns `(right, predicate, partition_keys, order_keys, cap)`.
    """
    body = spec.get("body")
    if not isinstance(body, dict):
        raise PayloadError("a lateral join needs a `( … )` body")
    where = body.get("where")
    left_ref = right_ref = None
    residual_where: dict | None = where if isinstance(where, dict) else None
    if isinstance(where, dict):
        left_ref, right_ref, residual_where = _hoist_correlation(where, frames)
    # The correlated equality is not a filter of the body; the join carries it.
    rest = {k: v for k, v in body.items() if k != "where"}
    if residual_where is not None:
        rest["where"] = residual_where
    on_left, on_right = spec.get("left"), spec.get("right")
    keys = [ref for ref in (left_ref, on_left) if isinstance(ref, str) and ref]
    cap = body.get("limit")
    order_specs = body.get("orderBy") or []
    if cap is None or not keys:
        # An uncorrelated `( … )` body keeps its own global `\limit` and `\order`.
        right = build(con, rest, ctes, tables)
        order_specs, cap = [], None
    else:
        # A per-left-row cap: the ordering has to be named to be counted from.
        if not order_specs:
            raise PayloadError(
                "a lateral body that caps rows needs \\order — the cap is per left row"
            )
        right = build(
            con,
            {k: v for k, v in rest.items() if k not in {"limit", "orderBy"}},
            ctes,
            tables,
        )
    parts: list[ir.Value] = []
    if left_ref is not None:
        parts.append(
            col(frames, left_ref, "lateral key") == _body_column(right, right_ref, "lateral key")
        )
    if on_left:
        parts.append(
            col(frames, on_left, "lateral key") == _body_column(right, on_right, "lateral key")
        )
    predicate: object = True if not parts else parts[0] if len(parts) == 1 else functools.reduce(
        operator.and_, parts
    )
    orders: list[tuple[ir.Column, str]] = []
    for item in order_specs:
        if not isinstance(item, dict):
            raise PayloadError("lateral order items must be objects")
        target = item.get("target")
        if not isinstance(target, str) or target not in right.columns:
            raise PayloadError(f"unknown order target in the lateral body: {target!r}")
        orders.append((right[target], str(item.get("direction") or "asc")))
    # The partition is the left row the correlation matched — its bare column
    # name, which is the name the joined relation keeps when the two sides share
    # one (ibis renames the right's copy).
    keys = [ref.rpartition(".")[2] for ref in keys]
    return right, predicate, keys, orders, cap


def _cap_lateral_rows(
    expr: ir.Table, keys: list[str], orders: list[tuple[ir.Column, str]], cap: int
) -> ir.Table:
    """Keep the first `cap` rows matched for each left row — LATERAL's `\\limit`.

    The rewrite every planner uses: number the joined rows inside the
    correlation's partition, in the body's own order, keep `row_number() <= cap`,
    then drop the numbering. The body's ordering columns belong to the right
    side, so they are re-projected under names of their own first — a window
    frame may only depend on one relation, and the join may have renamed a right
    column that shares its name with a left one.
    """
    helpers = [f"_d8r_order_{index}" for index in range(len(orders))]
    clash = next((name for name in (*helpers, _LATERAL_ROW) if name in expr.columns), "")
    if clash:
        raise PayloadError(f'column name "{clash}" collides with the lateral row cap')
    numbered = expr.select(
        [*[expr[name] for name in expr.columns], *[col.name(helper) for col, helper in zip([c for c, _ in orders], helpers)]]
    )
    window = ibis.window(
        group_by=[numbered[key] for key in keys],
        order_by=[
            numbered[helper].desc() if direction == "desc" else numbered[helper].asc()
            for (_, direction), helper in zip(orders, helpers)
        ],
    )
    numbered = numbered.mutate(**{_LATERAL_ROW: ibis.row_number().over(window) + 1})
    return numbered.filter(numbered[_LATERAL_ROW] <= cap).drop(*helpers, _LATERAL_ROW)


def _join_frames(
    con,
    dataset: object,
    joins: object,
    alias: object = None,
    ctes: dict[str, ir.Table] | None = None,
    body: object = None,
    tables: dict[str, ir.Table] | None = None,
) -> tuple[list[tuple[str, ir.Table]], ir.Table]:
    """Build the ordered open-table list and the chained inner-join expression.

    Frames are `[source, *joined]` in document order, keyed by identifier (alias if
    present, else dataset name); duplicate identifiers are rejected. Join keys
    validate against frames accumulated so far (left) and the right table;
    predicates reference the pre-join parent frames, which ibis still resolves
    inside the join graph. A source may be an inline subquery body, and a
    `lateral` join's body may read the frames to its left (see `_lateral_parts`).
    """
    source = _build_source(con, dataset, body, ctes, tables)
    identifier = alias if isinstance(alias, str) and alias else dataset
    frames: list[tuple[str, ir.Table]] = [(identifier, source)]
    expr: ir.Table = source
    if joins is None:
        joins = []
    if not isinstance(joins, list):
        raise PayloadError("joins must be an array")
    for spec in joins:
        if not isinstance(spec, dict):
            raise PayloadError("join specs must be objects")
        right_name = spec.get("dataset")
        right_alias = spec.get("alias")
        right_ident = right_alias if isinstance(right_alias, str) and right_alias else right_name
        if not isinstance(right_ident, str) or not right_ident:
            raise PayloadError("a join source needs an identifier")
        if any(name == right_ident for name, _ in frames):
            raise PayloadError(f'duplicate table identifier "{right_ident}"')
        if spec.get("lateral"):
            right, predicate, keys, orders, cap = _lateral_parts(con, spec, frames, ctes, tables)
            expr = expr.join(right, predicate)
            if cap is not None:
                expr = _cap_lateral_rows(expr, keys, orders, cap)
            frames.append((right_ident, right))
            continue
        right = _build_source(con, right_name, spec.get("body"), ctes, tables)
        # A *qualified* `on` side may name either relation — `a.k = b.k` and
        # `b.k = a.k` are the same equality — so it resolves against the
        # accumulated frames *and* this join's own table (its identifier is fresh,
        # duplicates refused above, so a qualified name is never ambiguous). A
        # *bare* name keeps left/right scoping: left resolves to the accumulated
        # left, right to this table, so `on user_id` is `left.user_id = right.user_id`,
        # never a self-comparison that would cross the two relations.
        both = [*frames, (right_ident, right)]

        def key(ref: object, own: list[tuple[str, ir.Table]]) -> ir.Column:
            scoped = both if isinstance(ref, str) and "." in ref else own
            return col(scoped, ref, "join key")

        left_column = key(spec.get("left"), frames)
        right_column = key(spec.get("right"), [(right_ident, right)])
        expr = expr.join(right, left_column == right_column)
        frames.append((right_ident, right))
    return frames, expr


def _coerce(value: object, dtype) -> object:
    """Coerce a comparison literal to match the column dtype."""
    if dtype.is_numeric():
        if isinstance(value, bool):
            raise PayloadError(f"cannot compare numeric column to {value!r}")
        if isinstance(value, (int, float)):
            return value
        text = str(value).strip()
        try:
            return int(text)
        except ValueError:
            pass
        try:
            return float(text)
        except ValueError:
            raise PayloadError(f"cannot compare numeric column to {value!r}") from None
    return value if isinstance(value, str) else str(value)


def _aggregate(
    con, expr: ir.Table, frames: list[tuple[str, ir.Table]], spec: object,
    ctes: dict[str, ir.Table] | None, tables: dict[str, ir.Table] | None,
) -> ir.Scalar:
    """Apply an optional predicate to this reduction, never to the input relation."""
    if not isinstance(spec, dict) or not {"fn", "arg"} <= set(spec) or set(spec) - {"fn", "arg", "where"}:
        raise PayloadError("aggregate must contain fn, arg and optional where")
    fn, arg = spec["fn"], spec["arg"]
    if not isinstance(fn, str) or fn not in AGGREGATE_FNS:
        raise PayloadError(f"unknown aggregate: {fn!r}")
    if not isinstance(arg, str):
        raise PayloadError("aggregate arguments must be a column or count(*)")
    predicate = spec.get("where")
    if predicate is not None:
        _validate_aggregate_where(predicate)
        predicate = _where_expr(con, frames, predicate, ctes, tables)
    if arg == "*":
        if fn != "count":
            raise PayloadError(f"{fn}(*) is not supported — only count accepts '*'")
        if predicate is not None:
            # Table.count's lightweight binding does not rewrite ancestor fields.
            # Dereference onto the joined/filtered relation before CountStar owns it.
            (predicate,) = expr.bind(predicate)
        return expr.count(where=predicate)
    column = col(frames, arg)
    method = "mean" if fn == "avg" else fn
    try:
        return getattr(column, method)(where=predicate)
    except (IbisError, AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise PayloadError(f"invalid {fn} operand: {exc}") from exc


def _validate_aggregate_where(predicate: object) -> None:
    """Reject malformed/ambiguous raw predicate trees before Ibis sees them."""
    pending = [(predicate, 0, True)]
    nodes = 0
    leaf_fields = {"column", "op", "value", "subquery", "low", "high", "values", "group"}
    scalar_types = (str, int, float, bool)
    while pending:
        condition, depth, tree = pending.pop()
        nodes += 1
        if depth > MAX_WHERE_DEPTH or nodes > MAX_WHERE_NODES:
            raise PayloadError("aggregate where exceeds maximum depth or nodes")
        allowed = leaf_fields | {"ands", "ors"} if tree else leaf_fields
        if not isinstance(condition, dict) or set(condition) - allowed:
            raise PayloadError("aggregate where conditions must be predicate objects")
        for key in ("ands", "ors"):
            if key not in condition:
                continue
            children = condition[key]
            if not isinstance(children, list) or not children:
                raise PayloadError(f"aggregate where {key} must be a non-empty array")
            if key == "ands":
                pending.extend((child, depth, False) for child in children)
            else:
                for group in children:
                    if not isinstance(group, list) or not group:
                        raise PayloadError("aggregate where or-groups must be non-empty arrays")
                    pending.extend((child, depth, False) for child in group)
        if "group" in condition:
            if any(condition.get(key) not in (None, "") for key in ("column", "op", "value", "subquery")) or any(key in condition for key in ("low", "high", "values")):
                raise PayloadError("aggregate where group cannot carry a predicate operand")
            pending.append((condition["group"], depth + 1, True))
            continue
        op = condition.get("op")
        if not isinstance(op, str) or op not in OPERATORS:
            raise PayloadError(f"unknown operator: {op!r}")
        if not isinstance(condition.get("column"), str):
            raise PayloadError("aggregate where needs a column")
        subquery = condition.get("subquery")
        if subquery is not None:
            if not isinstance(subquery, dict) or op not in SUBQUERY_OPS | COMPARISONS.keys():
                raise PayloadError("aggregate where has an invalid subquery operand")
            operands = set()
        elif op in NULL_OPS:
            operands = set()
        elif op in BETWEEN_OPS:
            operands = {"low", "high"}
        elif op in SUBQUERY_OPS:
            operands = {"values"}
        else:
            operands = {"value"}
        if (set(condition) & {"low", "high", "values"}) != operands - {"value"}:
            raise PayloadError("aggregate where has invalid predicate operands")
        if "value" not in operands and condition.get("value") not in (None, ""):
            raise PayloadError("aggregate where operator takes no value operand")
        for key in operands:
            if key not in condition:
                raise PayloadError(f"aggregate where needs {key}")
            value = condition[key]
            if key == "values":
                if not isinstance(value, list) or not value:
                    raise PayloadError("aggregate where values must be a non-empty array")
                values = value
            else:
                values = [value]
            if any(value is not None and not isinstance(value, scalar_types) for value in values):
                raise PayloadError("aggregate where operands must be scalar values")


def _predicate(frames: list[tuple[str, ir.Table]], condition: dict) -> ir.BooleanValue:
    """Boolean predicate from a condition object (shared by `where` and case
    `when` branches).

    The base shape is `{column, op, value}`; `between` carries `low`/`high`,
    an `in`/`not in` literal list carries `values`, and `is [not] null` carries
    neither. A `{subquery}` operand is handled one level up (`_apply_filter`),
    never inside a `\\case` branch.
    """
    column = col(frames, condition.get("column"))
    op = condition.get("op")
    value = condition.get("value")
    if op in NULL_OPS:
        return column.isnull() if op == "is null" else column.notnull()
    if op in BETWEEN_OPS:
        low = _coerce(condition.get("low"), column.type())
        high = _coerce(condition.get("high"), column.type())
        return column.between(low, high)
    if op in SUBQUERY_OPS:
        values = condition.get("values")
        if not isinstance(values, list):
            raise PayloadError(f"`{op}` needs a list of values or a subquery")
        coerced = [None if v is None else _coerce(v, column.type()) for v in values]
        return column.isin(coerced) if op == "in" else column.notin(coerced)
    if op in ("like", "ilike"):
        if not isinstance(column, ir.StringValue):
            raise PayloadError(
                f"`{op}` needs a string column: {column.get_name()!r} is {column.type()}"
            )
        # SQL's own pattern match: `%`/`_` are wildcards and the operand is
        # passed through untouched — no `%` stripping, no containment.
        pattern = str(value)
        return column.ilike(pattern) if op == "ilike" else column.like(pattern)
    if op in REGEX_OPS:
        return _regex_match(column, op, value)
    compare = COMPARISONS.get(op)
    if compare is None:
        raise PayloadError(f"unknown operator: {op!r}")
    return compare(column, _coerce(value, column.type()))


def _regex_match(column: ir.Column, op: str, pattern: object) -> ir.BooleanValue:
    """`<column> ~ <pattern>` / `!~ <pattern>` — POSIX matching off the dtype.

    Regex is a string operation, so a non-string column is the user's mistake
    and says so instead of surfacing an ibis attribute error.
    """
    if not isinstance(column, ir.StringValue):
        raise PayloadError(
            f"`{op}` needs a string column: {column.get_name()!r} is {column.type()}"
        )
    text = str(pattern)
    match = column.re_search(text)
    return match if op == "~" else ~match


def _apply_filter(
    con,
    expr: ir.Table,
    frames: list[tuple[str, ir.Table]],
    condition: object,
    ctes: dict[str, ir.Table] | None,
    tables: dict[str, ir.Table] | None,
) -> ir.Table:
    """A `\\where` clause: a literal predicate, a predicate tree, or one over
    an inline subquery.

    A single plain condition filters exactly as it always did; composition
    (`ands`/`ors`, and `( ... )` groups under `group`) reduces through
    `_where_expr`."""
    if not isinstance(condition, dict):
        raise PayloadError("where must be an object")
    return expr.filter(_where_expr(con, frames, condition, ctes, tables))


def _head(group: object) -> dict:
    """An or-group's `[first, *ands]` list as one and-group dict."""
    if not isinstance(group, list) or not group or not isinstance(group[0], dict):
        raise PayloadError("where or-groups must be non-empty arrays of conditions")
    return {**group[0], "ands": group[1:]}


def _where_expr(
    con,
    frames: list[tuple[str, ir.Table]],
    condition: dict,
    ctes: dict[str, ir.Table] | None,
    tables: dict[str, ir.Table] | None,
) -> object:
    """A where-tree dict (a clause or a group) as a boolean expression.

    Same shape `_apply_filter` reads at the top level: the head's and-group
    ANDed, then `ors` ORed over it. A group node recurses through its own
    `group` subtree — parens are just a where-tree in a box."""
    if not isinstance(condition, dict):
        raise PayloadError("where conditions must be objects")
    ors = condition.get("ors")
    if ors is not None:
        if not isinstance(ors, list) or not ors:
            raise PayloadError("where or-groups must be a non-empty array")
        groups = [condition, *(_head(group) for group in ors)]
        return functools.reduce(
            operator.or_,
            [_condition_tree(con, frames, group, ctes, tables) for group in groups],
        )
    return _condition_tree(con, frames, condition, ctes, tables)


def _condition_tree(
    con,
    frames: list[tuple[str, ir.Table]],
    condition: dict,
    ctes: dict[str, ir.Table] | None,
    tables: dict[str, ir.Table] | None,
) -> object:
    """One and-group as a boolean expression; its leaves AND together.

    Each leaf is a plain predicate, a subquery comparison — a subquery
    inside composition is still a filter leaf, never a second relation —
    or a parenthesized group, whose own tree hangs under `group`."""
    leaves: list[object] = []
    for cond in [condition, *(condition.get("ands") or [])]:
        if not isinstance(cond, dict):
            raise PayloadError("where conditions must be objects")
        group = cond.get("group")
        if group is not None:
            leaves.append(_where_expr(con, frames, group, ctes, tables))
        elif cond.get("subquery") is not None:
            leaves.append(_subquery_predicate(con, frames, cond, cond["subquery"], ctes, tables))
        else:
            leaves.append(_predicate(frames, cond))
    return leaves[0] if len(leaves) == 1 else functools.reduce(operator.and_, leaves)


def _subquery_predicate(
    con,
    frames: list[tuple[str, ir.Table]],
    condition: dict,
    subquery: object,
    ctes: dict[str, ir.Table] | None,
    tables: dict[str, ir.Table] | None,
) -> object:
    """A predicate over an inline `( … )` subquery operand.

    `in`/`not in` take the subquery's single column as the set; a comparison
    takes it as a scalar subquery (`\\where amount > ( … )`), which needs exactly
    one column too — anything else is the user's mistake, named as such.
    """
    if not isinstance(subquery, dict):
        raise PayloadError("where subquery must be an object")
    op = condition.get("op")
    column = col(frames, condition.get("column"))
    sub = build(con, subquery, ctes, tables)
    projected = [str(name) for name in sub.columns]
    if len(projected) != 1:
        raise PayloadError(
            f"`{op}` needs a subquery of exactly one column — "
            f"it projects: {', '.join(projected) or '(nothing)'}"
        )
    inner = sub[projected[0]]
    if op in SUBQUERY_OPS:
        return column.isin(inner) if op == "in" else column.notin(inner)
    compare = COMPARISONS.get(op)
    if compare is None:
        raise PayloadError(f"unknown operator: {op!r}")
    return compare(column, inner.as_scalar())


def _literal(value: object) -> ir.Value:
    """A coerced then/else literal: numeric-looking strings become numbers."""
    text = value.strip() if isinstance(value, str) else value
    if isinstance(text, str) and text:
        try:
            return ibis.literal(int(text))
        except ValueError:
            pass
        try:
            return ibis.literal(float(text))
        except ValueError:
            pass
    return ibis.literal(value)


def _case_column(frames: list[tuple[str, ir.Table]], spec: object) -> ir.Column:
    """A `cases` entry -> `CASE WHEN ... THEN ... [ELSE ...] END AS alias`."""
    if not isinstance(spec, dict):
        raise PayloadError("case specs must be objects")
    alias = spec.get("alias")
    if not isinstance(alias, str) or not alias:
        raise PayloadError("case must have a non-empty alias")
    whens = spec.get("whens")
    if not isinstance(whens, list) or not whens:
        raise PayloadError(f"case {alias!r} must have at least one when")
    branches = []
    for when in whens:
        if not isinstance(when, dict):
            raise PayloadError("case when branches must be objects")
        condition = _predicate(frames, when)
        then = when.get("then")
        if then is None:
            raise PayloadError(f"case {alias!r} when branch needs a then value")
        branches.append((condition, _literal(then)))
    default = spec.get("else") if "else" in spec else spec.get("else_")
    expr = ibis.cases(*branches, else_=_literal(default) if default is not None else None)
    return expr.name(alias)


RANK_FNS = frozenset({"rank", "dense_rank", "row_number"})

# `\union`/`\intersect`/`\except` -> the ibis set operation on the accumulated
# left (`\except` is SQL's EXCEPT, ibis' `difference`).
SET_OP_METHODS = {"union": "union", "intersect": "intersect", "except": "difference"}


def _frame_offset(bound: object, edge: str) -> int | None:
    """A canonical frame bound as ibis' own offset convention.

    `None` means unbounded on that edge, `0` is the current row, and negative /
    positive numbers count rows (`rows`) or ordering values (`range`) backwards
    and forwards — the convention `ibis.window(rows=…, range=…)` reads.
    """
    text = str(bound).strip().lower()
    if text.startswith("unbounded"):
        return None
    if text == "current row":
        return 0
    number, _, side = text.partition(" ")
    if not number.isdigit() or side not in ("preceding", "following"):
        raise PayloadError(f"unknown frame bound: {bound!r} at the {edge} of the frame")
    return -int(number) if side == "preceding" else int(number)


def _window_frame(expr: ir.Table, frames: list[tuple[str, ir.Table]], spec: object):
    """A select item's `window` object -> an ibis window (>= 1 of partition/order)."""
    if not isinstance(spec, dict):
        raise PayloadError("window must be an object")
    partitions = spec.get("partitionBy") or []
    if not isinstance(partitions, list):
        raise PayloadError("window partitionBy must be an array")
    # Bind the resolved column expressions, not their bare names: a join can
    # rename duplicate columns, and each window must depend on one relation.
    group_by = expr.bind([col(frames, name, "partition column") for name in partitions])
    order = spec.get("order")
    order_by = None
    if order is not None:
        if not isinstance(order, dict):
            raise PayloadError("window order must be an object")
        direction = order.get("direction", "asc")
        if direction not in ("asc", "desc"):
            raise PayloadError(f"unknown sort direction: {direction!r}")
        (key,) = expr.bind(col(frames, order.get("column"), "order column"))
        order_by = key.desc() if direction == "desc" else key.asc()
    if not group_by and order_by is None:
        raise PayloadError("window needs partitionBy and/or order")
    bounds = spec.get("frame")
    if bounds is None:
        return ibis.window(group_by=group_by, order_by=order_by)
    if not isinstance(bounds, dict):
        raise PayloadError("window frame must be an object")
    kind = bounds.get("kind")
    if kind not in ("rows", "range"):
        raise PayloadError(f"unknown window frame kind: {kind!r}")
    span = (_frame_offset(bounds.get("start"), "start"), _frame_offset(bounds.get("end"), "end"))
    # `rows` counts rows, `range` counts ordering values — ibis spells both with
    # the same offsets, so the frame's kind is what picks the keyword.
    return ibis.window(
        group_by=group_by,
        order_by=order_by,
        **({"rows": span} if kind == "rows" else {"range": span}),
    )


def _windowed_column(
    con, expr: ir.Table, frames: list[tuple[str, ir.Table]], item: dict, name: str,
    ctes: dict[str, ir.Table] | None, tables: dict[str, ir.Table] | None,
) -> ir.Column:
    """A windowed select item -> `<fn> OVER ( … ) AS name`.

    Two shapes: a rank call (`rank()` / `dense_rank()` / `row_number()` — no
    argument), or a capability aggregate over a frame (a partition total).
    Ranks render 1-based: ibis' SQL dialects normalize rank functions to
    0-based, so the compiled expression adds one back."""
    window = _window_frame(expr, frames, item.get("window"))
    rank = item.get("rank")
    if rank is not None:
        if not isinstance(rank, dict):
            raise PayloadError("rank must be an object")
        fn = rank.get("fn")
        if fn not in RANK_FNS:
            raise PayloadError(f"unknown rank function: {fn!r}")
        column = getattr(ibis, fn)().over(window) + 1
        return column.name(name)
    aggregate = item.get("aggregate")
    if not isinstance(aggregate, dict):
        raise PayloadError("over (...) needs an aggregate or rank function")
    (aggregate_expr,) = expr.bind(
        _aggregate(con, expr, frames, aggregate, ctes, tables)
    )
    return aggregate_expr.over(window).name(name)


def _apply_set_ops(
    con,
    expr: ir.Table,
    set_ops: object,
    ctes: dict[str, ir.Table] | None,
    tables: dict[str, ir.Table] | None,
) -> ir.Table:
    """Apply `setOps` left-deep, in document order, onto the built left query.

    The accumulated left's output columns define what each operation is over:
    the operand is projected to exactly those columns (in that order), so extra
    operand columns are dropped and a column the operand lacks is the user-level
    error below. `distinct` is SQL's default for all three — `\\union all`
    keeps duplicates. Nullability-only differences widen to nullable without
    changing values; genuinely different types still surface as a `PayloadError`,
    so no set-operation mistake reaches the UI as an internal error.
    """
    if set_ops is None:
        return expr
    if not isinstance(set_ops, list):
        raise PayloadError("setOps must be an array")
    for spec in set_ops:
        if not isinstance(spec, dict):
            raise PayloadError("setOp specs must be objects")
        op = spec.get("op")
        method = SET_OP_METHODS.get(op)
        if method is None:
            raise PayloadError(f"unknown set operation: {op!r}")
        dataset = spec.get("dataset")
        label = dataset if isinstance(dataset, str) and dataset else "( … )"
        distinct = spec.get("distinct", True)
        if not isinstance(distinct, bool):
            raise PayloadError("setOp distinct must be a boolean")
        right = _build_source(con, dataset, spec.get("body"), ctes, tables)
        target = [str(name) for name in expr.columns]
        operand = [str(name) for name in right.columns]
        missing = [name for name in target if name not in operand]
        if missing:
            raise PayloadError(
                f'\\{op} "{label}" is missing {", ".join(missing)} — '
                f'it projects: {", ".join(operand)}'
            )
        # ibis' set operations align by name and need equal schemas: the operand
        # is projected to the left's columns, in the left's order.
        if operand != target:
            right = right.select(target)
        try:
            # Ibis requires exact schema equality, including nullability. Widen
            # only that flag; never coerce different value types or fill NULLs.
            left_casts, right_casts = {}, {}
            right_schema = right.schema()
            for name, left_type in expr.schema().items():
                right_type = right_schema[name]
                if left_type == right_type or left_type.copy(nullable=right_type.nullable) != right_type:
                    continue
                if left_type.nullable:
                    right_casts[name] = right[name].cast(left_type)
                else:
                    left_casts[name] = expr[name].cast(right_type)
            if left_casts:
                expr = expr.mutate(left_casts)
            if right_casts:
                right = right.mutate(right_casts)
            expr = getattr(expr, method)(right, distinct=distinct)
        except IbisError as exc:
            raise PayloadError(f'\\{op} "{label}": {" ".join(str(exc).split())}') from exc
    return expr


def _regex_column(frames: list[tuple[str, ir.Table]], spec: dict, name: str) -> ir.Column:
    """A regex select item -> `REGEXP_EXTRACT` / `REGEXP_REPLACE` over a string column.

    `regexp_extract` returns the whole match unless a capture group is named;
    `regexp_replace` substitutes every match. Both are row-wise string
    operations, so a non-string column is the user's mistake, named as such.
    """
    fn = spec.get("fn")
    pattern = spec.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise PayloadError(f"{fn} needs a pattern")
    column = col(frames, spec.get("arg"), "regex argument")
    if not isinstance(column, ir.StringValue):
        raise PayloadError(
            f"{fn} needs a string column: {column.get_name()!r} is {column.type()}"
        )
    if fn == "regexp_extract":
        group = spec.get("group")
        return column.re_extract(pattern, group if isinstance(group, int) else 0).name(name)
    if fn == "regexp_replace":
        replacement = spec.get("replacement")
        return column.re_replace(pattern, replacement if isinstance(replacement, str) else "").name(name)
    raise PayloadError(f"unknown regex function: {fn!r}")


def _select_expression(
    con, expr: ir.Table, frames: list[tuple[str, ir.Table]], spec: object, kind: str,
    ctes: dict[str, ir.Table] | None, tables: dict[str, ir.Table] | None,
) -> tuple[ir.Value, str]:
    """Build a bounded expression and track its constant/row/aggregate dependency.

    Aggregate leaves are permitted only outside catalog scalar calls. Tracking
    dependencies explicitly prevents Ibis from turning mixed row/reduction math
    into an implicit window expression.
    """
    expected = {"op", "args"} if kind == "arithmetic" else {"fn", "args"}
    if not isinstance(spec, dict) or set(spec) != expected:
        raise PayloadError(f"{kind} must be an object with only {' and '.join(sorted(expected))}")
    nodes = 0

    def visit(node: object, depth: int, allow_aggregate: bool) -> tuple[ir.Value, str]:
        nonlocal nodes
        nodes += 1
        if depth > MAX_EXPRESSION_DEPTH:
            raise PayloadError(f"expression exceeds maximum depth {MAX_EXPRESSION_DEPTH}")
        if nodes > MAX_EXPRESSION_NODES:
            raise PayloadError(f"expression exceeds maximum nodes {MAX_EXPRESSION_NODES}")
        if not isinstance(node, dict):
            raise PayloadError("expression operands must be objects")
        fields = set(node)
        if fields == {"column"}:
            return col(frames, node["column"], "expression operand"), "row"
        if fields == {"literal"}:
            literal = node["literal"]
            if not isinstance(literal, dict) or set(literal) != {"value"}:
                raise PayloadError("expression literal must be an object with only a value")
            value = literal["value"]
            if value is not None and not isinstance(value, (str, int, float, bool)):
                raise PayloadError("literal value must be a string, number, boolean, or null")
            try:
                return ibis.literal(value), "constant"
            except (IbisError, TypeError, ValueError, OverflowError) as exc:
                raise PayloadError(f"invalid expression literal: {exc}") from exc
        if fields == {"fn", "args"}:
            value = _scalar_call(node, lambda child: visit(child, depth + 1, False)[0])
            return value, "row" if isinstance(value, ir.Column) else "constant"
        if fields == {"aggregate"}:
            if not allow_aggregate:
                raise PayloadError("aggregate operands are not supported inside scalar calls; use a CTE")
            return _aggregate(con, expr, frames, node["aggregate"], ctes, tables), "aggregate"
        if fields != {"op", "args"}:
            raise PayloadError("expression operand must contain only column, literal, aggregate, fn and args, or op and args")
        op, args = node["op"], node["args"]
        if not isinstance(op, str) or op not in ARITHMETIC_PRECEDENCE:
            raise PayloadError(f"unknown arithmetic operator: {op!r}")
        if not isinstance(args, list):
            raise PayloadError("arithmetic args must be an array")
        if len(args) != 2 and not (len(args) == 1 and op in ("+", "-")):
            raise PayloadError(f"arithmetic {op!r} needs two operands (unary + and - accept one)")
        values = []
        dependencies = set()
        for arg in args:
            value, dependency = visit(arg, depth + 1, allow_aggregate)
            dtype = value.type()
            if dtype.is_null():
                value = value.cast("int64")
            elif not dtype.is_numeric() or dtype.is_boolean():
                raise PayloadError(f"arithmetic {op!r} needs numeric operands, got {dtype}")
            values.append(value)
            dependencies.add(dependency)
        if "aggregate" in dependencies and "row" in dependencies:
            raise PayloadError("arithmetic cannot mix aggregates and unaggregated columns; use a CTE")
        dependency = "aggregate" if "aggregate" in dependencies else "row" if "row" in dependencies else "constant"
        try:
            if len(values) == 1:
                value = values[0] if op == "+" else -values[0]
            else:
                left, right = values
                if op == "/":
                    # Ibis true division casts integer operands where required;
                    # NULLIF makes zero divisors consistent across SQL backends.
                    value = left / right.nullif(0)
                elif op == "+":
                    value = left + right
                elif op == "-":
                    value = left - right
                else:
                    value = left * right
        except (IbisError, TypeError, ValueError, OverflowError) as exc:
            raise PayloadError(f"invalid arithmetic {op!r}: {exc}") from exc
        return value, dependency

    return visit(spec, 1, True)


def _scalar_call(spec: object, resolve: Callable[[object], ir.Value]) -> ir.Value:
    """Build a catalog-whitelisted call using Ibis' own scalar semantics."""
    if not isinstance(spec, dict) or set(spec) != {"fn", "args"}:
        raise PayloadError("scalar must be an object with only fn and args")
    fn = spec["fn"]
    if not isinstance(fn, str) or fn not in SCALAR_FUNCTIONS:
        raise PayloadError(f"unknown scalar function: {fn!r}")
    signature = SCALAR_FUNCTIONS[fn]
    nodes = spec["args"]
    if not isinstance(nodes, list):
        raise PayloadError(f"{fn} args must be an array")
    if not signature.accepts(len(nodes)):
        maximum = len(signature.parameters)
        expected = (
            f"at least {signature.minimum}"
            if signature.variadic
            else str(maximum) if signature.minimum == maximum
            else f"{signature.minimum} to {maximum}"
        )
        raise PayloadError(f"{fn} expects {expected} arguments, got {len(nodes)}")
    args = []
    for index, node in enumerate(nodes):
        value = resolve(node)
        kind = signature.argument_kind(index)
        dtype = value.type()
        if kind != "any":
            if dtype.is_null():
                value = value.cast("int64" if kind == "integer" else "string")
            elif not (dtype.is_string() if kind == "string" else dtype.is_integer()):
                raise PayloadError(f"{fn} argument {index + 1} needs {kind}, got {dtype}")
        args.append(value)
    first, *rest = args
    try:
        if fn == "string":
            return first.cast("string")
        if fn == "concat":
            return first.concat(*rest)
        if fn == "concat_ws":
            return first.join(rest)
        if fn.startswith("url_"):
            # ibis spells the URL accessors bare (`s.host()`); the catalog
            # keeps the qualified name so completion never shadows `path`-style words.
            return getattr(first, fn.removeprefix("url_"))()
        return getattr(first, fn)(*rest)
    except (IbisError, TypeError, ValueError, OverflowError) as exc:
        raise PayloadError(f"invalid {fn} arguments: {exc}") from exc


def _scalar_subquery_column(
    con,
    subquery: object,
    name: str | None,
    ctes: dict[str, ir.Table] | None,
    tables: dict[str, ir.Table] | None,
) -> ir.Column:
    """A `\\select ( … ) as <alias>` scalar subquery, projected as one column.

    It projects exactly one column (the same rule SQL has) and needs an alias —
    there is nothing to derive a name from. It is a computed column like a case
    or window column, so the caller keeps it out of any grouped projection.
    """
    if not name:
        raise PayloadError("a scalar subquery needs an alias — write ( … ) as <name>")
    if not isinstance(subquery, dict):
        raise PayloadError("a scalar subquery must be an object")
    sub = build(con, subquery, ctes, tables)
    projected = [str(column) for column in sub.columns]
    if len(projected) != 1:
        raise PayloadError(
            "a scalar subquery must project exactly one column — "
            f"it projects: {', '.join(projected) or '(nothing)'}"
        )
    return sub[projected[0]].as_scalar().name(name)


def _star_columns(frames: list[tuple[str, ir.Table]]) -> list[ir.Column]:
    """Empty-select projection with joins: left table first, skipping duplicate names."""
    columns: list[ir.Column] = []
    seen: set[str] = set()
    for _, table in frames:
        for name in table.columns:
            if name not in seen:
                seen.add(name)
                columns.append(table[name])
    return columns


def build(
    con,
    payload: dict,
    ctes: dict[str, ir.Table] | None = None,
    tables: dict[str, ir.Table] | None = None,
) -> ir.Table:
    """Compile an ExecutePayload dict into an Ibis table expression.

    Empty `select` means all columns (with joins: left table first, each joined
    table skipping names already projected). Tables may carry aliases; frames key
    on the alias when present (strict — see `col`). Aggregates group by `groupBy`
    keys plus any plain select columns (implicit grouping); an aggregate without an
    explicit alias gets the derived name from `_derived_alias`. `orderBy.target`
    resolves against the output columns (explicit and derived aliases are baked in
    by then); qualified targets fall back to their bare column name. A select item
    with `window` becomes an OVER expression (aggregate partition total, or a
    1-based rank) appended after the projection; `cases` append computed columns
    too; `ctes` bodies build recursively and their names shadow registered
    datasets."""
    if not isinstance(payload, dict):
        raise PayloadError("payload must be an object")
    distinct = payload.get("distinct", False)
    if not isinstance(distinct, bool):
        raise PayloadError("distinct must be a boolean")

    cte_tables: dict[str, ir.Table] = dict(ctes) if ctes else {}
    _build_ctes(con, payload.get("ctes"), cte_tables, tables)

    frames, expr = _join_frames(
        con,
        payload.get("dataset"),
        payload.get("joins"),
        payload.get("alias"),
        cte_tables,
        payload.get("body"),
        tables,
    )

    select = payload.get("select") or []
    group_by = payload.get("groupBy") or []
    where = payload.get("where")
    order_by = payload.get("orderBy") or []
    limit = payload.get("limit")

    if not isinstance(select, list):
        raise PayloadError("select must be an array")
    if not isinstance(group_by, list):
        raise PayloadError("groupBy must be an array")
    if not isinstance(order_by, list):
        raise PayloadError("orderBy must be an array")

    if where is not None:
        expr = _apply_filter(con, expr, frames, where, cte_tables, tables)

    plain: list[ir.Value] = []
    constants: list[ir.Scalar] = []
    aggregates: list[ir.Scalar] = []
    windowed: list[ir.Column] = []
    scalar_subqueries: list[ir.Column] = []
    # Output names already projected as plain columns; a star expansion skips
    # these so `\select user_id, *` never duplicates `user_id`.
    projected: set[str] = set()
    star_seen = False
    for item in select:
        if not isinstance(item, dict):
            raise PayloadError("select items must be objects")
        column_name = item.get("column")
        literal = item.get("literal")
        scalar = item.get("scalar")
        arithmetic = item.get("arithmetic")
        aggregate = item.get("aggregate")
        temporal = item.get("temporal")
        rank = item.get("rank")
        regex = item.get("regex")
        subquery = item.get("subquery")
        alias = item.get("alias")
        named = alias if isinstance(alias, str) and alias else None
        star = bool(item.get("star"))
        if scalar is not None and any(
            value is not None
            for value in (column_name, literal, arithmetic, aggregate, temporal, rank, regex, subquery)
        ):
            raise PayloadError("a scalar select item carries only one expression")
        if arithmetic is not None and any(
            value is not None
            for value in (column_name, literal, aggregate, temporal, rank, regex, subquery)
        ):
            raise PayloadError("an arithmetic select item carries only one expression")
        if star:
            if item.get("window") is not None:
                raise PayloadError("a star select cannot carry over (...)")
            if named:
                raise PayloadError("a star select cannot take an alias")
            if (
                column_name is not None
                or aggregate is not None
                or temporal is not None
                or rank is not None
                or regex is not None
                or subquery is not None
                or literal is not None
                or scalar is not None
                or arithmetic is not None
            ):
                raise PayloadError("a star select item carries no expression")
            star_seen = True
            for column in _star_columns(frames):
                if column.get_name() not in projected:
                    projected.add(column.get_name())
                    plain.append(column)
        elif item.get("window") is not None:
            if scalar is not None:
                raise PayloadError("a scalar call cannot carry over (...)")
            if arithmetic is not None:
                raise PayloadError("arithmetic cannot carry over (...); use a CTE")
            if rank is not None:
                fn = rank.get("fn") if isinstance(rank, dict) else None
                windowed.append(_windowed_column(con, expr, frames, item, named or str(fn), cte_tables, tables))
            elif isinstance(aggregate, dict):
                fn = aggregate.get("fn")
                windowed.append(
                    _windowed_column(con, expr, frames, item, named or _derived_alias(fn, aggregate.get("arg")), cte_tables, tables)
                )
            else:
                raise PayloadError("over (...) needs an aggregate or rank function")
        elif scalar is not None or arithmetic is not None:
            kind = "arithmetic" if arithmetic is not None else "scalar"
            spec = arithmetic if arithmetic is not None else scalar
            out, dependency = _select_expression(con, expr, frames, spec, kind, cte_tables, tables)
            out = out.name(named or ("arithmetic" if arithmetic is not None else scalar["fn"]))
            if dependency == "aggregate":
                aggregates.append(out)
            else:
                plain.append(out)
                projected.add(out.get_name())
                if dependency == "constant":
                    constants.append(out)
        elif literal is not None:
            if not isinstance(literal, dict) or "value" not in literal:
                raise PayloadError("literal must be an object with a value")
            value = literal["value"]
            if value is not None and not isinstance(value, (str, int, float, bool)):
                raise PayloadError("literal value must be a string, number, boolean, or null")
            out = ibis.literal(value)
            if named:
                out = out.name(named)
            constants.append(out)
            plain.append(out)
            projected.add(out.get_name())
        elif subquery is not None:
            scalar_subqueries.append(_scalar_subquery_column(con, subquery, named, cte_tables, tables))
        elif regex is not None:
            if not isinstance(regex, dict):
                raise PayloadError("regex must be an object")
            name = named or _derived_alias(regex.get("fn"), regex.get("arg"))
            plain.append(_regex_column(frames, regex, name))
            projected.add(name)
        elif rank is not None:
            fn = rank.get("fn") if isinstance(rank, dict) else None
            raise PayloadError(f"{fn}() requires over (...)")
        elif aggregate is not None:
            if not isinstance(aggregate, dict):
                raise PayloadError("aggregate must be an object")
            fn = aggregate.get("fn")
            out = _aggregate(con, expr, frames, aggregate, cte_tables, tables)
            name = named or _derived_alias(fn, aggregate.get("arg"))
            aggregates.append(out.name(name))
        elif temporal is not None:
            if not isinstance(temporal, dict):
                raise PayloadError("temporal must be an object")
            fn = temporal.get("fn")
            column = col(frames, temporal.get("arg"), "temporal argument")
            family = _dtype_family(column.type())
            allowed = TEMPORAL_FNS.get(family or "")
            if allowed is None:
                raise PayloadError(f"unknown function: {fn!r} for {family or str(column.type())}")
            if fn not in allowed:
                raise PayloadError(
                    f"unknown function: {fn!r} for a {family} column (supports {', '.join(sorted(allowed))})"
                )
            out = getattr(column, fn)()
            name = named or _derived_alias(fn, temporal.get("arg"))
            plain.append(out.name(name))
            projected.add(name)
        elif column_name is not None:
            column = col(frames, column_name)
            if named:
                column = column.name(named)
            plain.append(column)
            projected.add(named or column.get_name())
        else:
            raise PayloadError(
                "select item must have column, literal, scalar, arithmetic, aggregate, temporal, rank, regex, subquery, or *"
            )

    keys = []
    seen: set[str] = set()
    for key_name in group_by:
        column = col(frames, key_name, "groupBy column")
        if column.get_name() not in seen:
            keys.append(column)
            seen.add(column.get_name())

    cases = payload.get("cases") or []
    if not isinstance(cases, list):
        raise PayloadError("cases must be an array")
    computed = [_case_column(frames, spec) for spec in cases]
    if windowed and (aggregates or group_by):
        raise PayloadError("windowed select items cannot mix with aggregates or group by")
    if star_seen and (aggregates or group_by):
        raise PayloadError("star select cannot mix with aggregates or group by")
    if scalar_subqueries and (aggregates or group_by):
        raise PayloadError("a scalar subquery cannot mix with aggregates or group by")
    computed += windowed + scalar_subqueries
    joined = len(frames) > 1
    if aggregates:
        if computed:
            raise PayloadError("case/window columns cannot mix with aggregates")
        for column in plain:
            if isinstance(column, ir.Scalar):
                continue  # Constants never introduce grouping, even on an empty input.
            if column.get_name() not in seen:
                keys.append(column)
                seen.add(column.get_name())
        expr = expr.group_by(keys).aggregate(aggregates) if keys else expr.aggregate(aggregates)
        if constants:
            expr = expr.mutate(constants)
    elif plain:
        expr = expr.select([*plain, *computed])
    elif joined:
        expr = expr.select([*_star_columns(frames), *computed])  # empty select -> deduped star
    elif computed:
        expr = expr.mutate(computed)
    # else: empty select on a single table -> all columns unchanged

    # Set operations run over the projection above, so a trailing `\order` and
    # `\limit` apply to the merged result (SQL's own reading of the document).
    expr = _apply_set_ops(con, expr, payload.get("setOps"), cte_tables, tables)
    if distinct:
        expr = expr.distinct()

    if order_by:
        sorts = []
        for item in order_by:
            if not isinstance(item, dict):
                raise PayloadError("orderBy items must be objects")
            target = item.get("target", item.get("column"))
            if isinstance(target, str) and "." in target and target not in expr.columns:
                target = target.rpartition(".")[2]  # qualified order target -> bare output name
            if not isinstance(target, str) or target not in expr.columns:
                raise PayloadError(f"unknown orderBy target: {target!r}")
            direction = item.get("direction", "asc")
            if direction not in ("asc", "desc"):
                raise PayloadError(f"unknown sort direction: {direction!r}")
            column = expr[target]
            sorts.append(column.desc() if direction == "desc" else column.asc())
        expr = expr.order_by(sorts)

    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise PayloadError("limit must be a non-negative integer")
        expr = expr.limit(limit)

    return expr


def _build_ctes(
    con, ctes: object, resolved: dict[str, ir.Table], tables: dict[str, ir.Table] | None
) -> None:
    """Build a relation's local CTEs in order, shadowing enclosing names.

    `build` owns a copy of the enclosing map: function-local definitions can
    reuse caller names without leaking into callers or sibling invocations.
    Only repeated names within this list are duplicates.
    """
    if ctes is None:
        return
    if not isinstance(ctes, list):
        raise PayloadError("ctes must be an array")
    names: set[str] = set()
    for spec in ctes:
        if not isinstance(spec, dict):
            raise PayloadError("cte specs must be objects")
        name = spec.get("name")
        if not isinstance(name, str) or not name:
            raise PayloadError("cte must have a non-empty name")
        if name in names:
            raise PayloadError(f'duplicate cte name "{name}"')
        body = spec.get("body")
        if not isinstance(body, dict):
            raise PayloadError(f"cte {name!r} must have a body object")
        if body.get("ctes"):
            raise PayloadError("nested CTEs are not supported")
        resolved[name] = build(con, body, resolved, tables)
        names.add(name)


def _derived_alias(fn: object, arg: object) -> str:
    """Auto-alias for an unaliased aggregate: `<arg>_<fn>`, lowercased and
    sanitized to [a-z0-9_]. Qualified args keep only their last segment
    (`users.amount` -> `amount_sum`)."""
    last = arg.rpartition(".")[2] if isinstance(arg, str) else ""
    stem = re.sub(r"[^a-z0-9_]", "_", last.lower()) or "arg"
    name = re.sub(r"[^a-z0-9_]", "_", str(fn).lower())
    return f"{stem}_{name}"


def column_name_of(aggregate: dict) -> str:
    arg = aggregate.get("arg")
    if isinstance(arg, str):
        return arg.rpartition(".")[2] or "arg"
    return "arg"


def compile_sql(expr: ir.Table, dialect: str | None = None) -> str:
    """Render the expression's SQL without executing it.

    `dialect` names any ibis-supported compile target (e.g. "postgres");
    None renders for the expression's own backend.
    """
    return str(ibis.to_sql(expr, dialect=dialect)) if dialect else str(ibis.to_sql(expr))
