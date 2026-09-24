"""Pure payload -> Ibis expression compilation for anyQL's Python engine.

No UI concerns live here: user-facing mistakes raise `PayloadError`, which the
caller surfaces to the user as-is.
"""

from __future__ import annotations

import re
from operator import eq, ge, gt, le, lt, ne

import ibis
import ibis.expr.types as ir
from ibis.common.exceptions import IbisError

__all__ = ["PayloadError", "AGGREGATE_FNS", "TEMPORAL_FNS", "OPERATORS", "col", "build", "compile_sql"]


class PayloadError(Exception):
    """User-level query error (unknown dataset/column/aggregation/op, bad limit)."""


AGGREGATE_FNS = frozenset({"sum", "avg", "count", "min", "max"})
COMPARISONS = {"=": eq, "!=": ne, ">": gt, ">=": ge, "<": lt, "<=": le}
OPERATORS = frozenset(COMPARISONS) | {"like"}

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
    con, dataset: object, ctes: dict[str, ir.Table] | None = None
) -> ir.Table:
    """Resolve a dataset: a built CTE shadows a registered table of the same name."""
    if not isinstance(dataset, str) or not dataset:
        raise PayloadError("dataset must be a non-empty string")
    if ctes and dataset in ctes:
        return ctes[dataset]
    try:
        return con.table(dataset)
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


def _join_frames(
    con,
    dataset: object,
    joins: object,
    alias: object = None,
    ctes: dict[str, ir.Table] | None = None,
) -> tuple[list[tuple[str, ir.Table]], ir.Table]:
    """Build the ordered open-table list and the chained inner-join expression.

    Frames are `[source, *joined]` in document order, keyed by identifier (alias if
    present, else dataset name); duplicate identifiers are rejected. Join keys
    validate against frames accumulated so far (left) and the right table;
    predicates reference the pre-join parent frames, which ibis still resolves
    inside the join graph.
    """
    source = get_table(con, dataset, ctes)
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
        right = get_table(con, right_name, ctes)
        right_ident = right_alias if isinstance(right_alias, str) and right_alias else right_name
        if any(name == right_ident for name, _ in frames):
            raise PayloadError(f'duplicate table identifier "{right_ident}"')
        left_key = spec.get("left")
        right_key = spec.get("right")
        left_table = next(
            (t for _, t in frames if isinstance(left_key, str) and left_key in t.columns),
            None,
        )
        if left_table is None:
            raise PayloadError(f"unknown join key: {left_key!r}")
        if not isinstance(right_key, str) or right_key not in right.columns:
            raise PayloadError(f"unknown join key: {right_key!r}")
        expr = expr.join(right, left_table[left_key] == right[right_key])
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


def _aggregate(column: ir.Column, fn: str) -> ir.Scalar:
    """Apply an aggregate method. Resolved by name so a string column can
    `count` without touching dtype-incompatible methods like `sum`."""
    method = {
        "sum": "sum",
        "avg": "mean",
        "count": "count",
        "min": "min",
        "max": "max",
    }[fn]
    return getattr(column, method)()


def _predicate(frames: list[tuple[str, ir.Table]], condition: dict) -> ir.BooleanValue:
    """Boolean predicate from a `{column, op, value}` condition (shared by
    `where` and case `when` branches)."""
    column = col(frames, condition.get("column"))
    op = condition.get("op")
    value = condition.get("value")
    if op == "like":
        substring = str(value).strip("%")
        return column.contains(substring)
    compare = COMPARISONS.get(op)
    if compare is None:
        raise PayloadError(f"unknown operator: {op!r}")
    return compare(column, _coerce(value, column.type()))


def _apply_filter(expr: ir.Table, frames: list[tuple[str, ir.Table]], condition: object) -> ir.Table:
    if not isinstance(condition, dict):
        raise PayloadError("where must be an object")
    return expr.filter(_predicate(frames, condition))


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


def _window_frame(frames: list[tuple[str, ir.Table]], spec: object):
    """A select item's `window` object -> an ibis window (>= 1 of partition/order)."""
    if not isinstance(spec, dict):
        raise PayloadError("window must be an object")
    partitions = spec.get("partitionBy") or []
    if not isinstance(partitions, list):
        raise PayloadError("window partitionBy must be an array")
    group_by = [col(frames, name, "partition column") for name in partitions]
    order = spec.get("order")
    order_by = None
    if order is not None:
        if not isinstance(order, dict):
            raise PayloadError("window order must be an object")
        direction = order.get("direction", "asc")
        if direction not in ("asc", "desc"):
            raise PayloadError(f"unknown sort direction: {direction!r}")
        key = col(frames, order.get("column"), "order column")
        order_by = key.desc() if direction == "desc" else key.asc()
    if not group_by and order_by is None:
        raise PayloadError("window needs partitionBy and/or order")
    return ibis.window(group_by=group_by, order_by=order_by)


def _windowed_column(
    frames: list[tuple[str, ir.Table]], item: dict, name: str
) -> ir.Column:
    """A windowed select item -> `<fn> OVER ( … ) AS name`.

    Two shapes: a rank call (`rank()` / `dense_rank()` / `row_number()` — no
    argument), or a capability aggregate over a frame (a partition total).
    Ranks render 1-based: ibis' SQL dialects normalize rank functions to
    0-based, so the compiled expression adds one back."""
    window = _window_frame(frames, item.get("window"))
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
    fn = aggregate.get("fn")
    if fn not in AGGREGATE_FNS:
        raise PayloadError(f"unknown aggregate: {fn!r}")
    column = col(frames, aggregate.get("arg"))
    return _aggregate(column, fn).over(window).name(name)


def _apply_set_ops(con, expr: ir.Table, set_ops: object, ctes: dict[str, ir.Table] | None) -> ir.Table:
    """Apply `setOps` left-deep, in document order, onto the built left query.

    The accumulated left's output columns define what each operation is over:
    the operand is projected to exactly those columns (in that order), so extra
    operand columns are dropped and a column the operand lacks is the user-level
    error below. `distinct` is SQL's default for all three — `\\union all`
    keeps duplicates. A schema conflict ibis still refuses (a column that exists
    on both sides with different types) surfaces as a `PayloadError` too, so no
    set-operation mistake reaches the UI as an internal error.
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
        distinct = spec.get("distinct", True)
        if not isinstance(distinct, bool):
            raise PayloadError("setOp distinct must be a boolean")
        right = get_table(con, dataset, ctes)
        target = [str(name) for name in expr.columns]
        operand = [str(name) for name in right.columns]
        missing = [name for name in target if name not in operand]
        if missing:
            raise PayloadError(
                f'\\{op} "{dataset}" is missing {", ".join(missing)} — '
                f'it projects: {", ".join(operand)}'
            )
        # ibis' set operations align by name and need equal schemas: the operand
        # is projected to the left's columns, in the left's order.
        if operand != target:
            right = right.select(target)
        try:
            expr = getattr(expr, method)(right, distinct=distinct)
        except IbisError as exc:
            raise PayloadError(f'\\{op} "{dataset}": {" ".join(str(exc).split())}') from exc
    return expr


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


def build(con, payload: dict, ctes: dict[str, ir.Table] | None = None) -> ir.Table:
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

    cte_tables: dict[str, ir.Table] = dict(ctes) if ctes else {}
    _build_ctes(con, payload.get("ctes"), cte_tables)

    frames, expr = _join_frames(
        con, payload.get("dataset"), payload.get("joins"), payload.get("alias"), cte_tables
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
        expr = _apply_filter(expr, frames, where)

    plain: list[ir.Column] = []
    aggregates: list[ir.Column] = []
    windowed: list[ir.Column] = []
    # Output names already projected as plain columns; a star expansion skips
    # these so `\select user_id, *` never duplicates `user_id`.
    projected: set[str] = set()
    star_seen = False
    for item in select:
        if not isinstance(item, dict):
            raise PayloadError("select items must be objects")
        column_name = item.get("column")
        aggregate = item.get("aggregate")
        temporal = item.get("temporal")
        rank = item.get("rank")
        alias = item.get("alias")
        named = alias if isinstance(alias, str) and alias else None
        star = bool(item.get("star"))
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
            ):
                raise PayloadError("a star select item carries no expression")
            star_seen = True
            for column in _star_columns(frames):
                if column.get_name() not in projected:
                    projected.add(column.get_name())
                    plain.append(column)
        elif item.get("window") is not None:
            if rank is not None:
                fn = rank.get("fn") if isinstance(rank, dict) else None
                windowed.append(_windowed_column(frames, item, named or str(fn)))
            elif isinstance(aggregate, dict):
                fn = aggregate.get("fn")
                windowed.append(
                    _windowed_column(frames, item, named or _derived_alias(fn, aggregate.get("arg")))
                )
            else:
                raise PayloadError("over (...) needs an aggregate or rank function")
        elif rank is not None:
            fn = rank.get("fn") if isinstance(rank, dict) else None
            raise PayloadError(f"{fn}() requires over (...)")
        elif aggregate is not None:
            if not isinstance(aggregate, dict):
                raise PayloadError("aggregate must be an object")
            fn = aggregate.get("fn")
            if fn not in AGGREGATE_FNS:
                raise PayloadError(f"unknown aggregate: {fn!r}")
            column = col(frames, aggregate.get("arg"))
            out = _aggregate(column, fn)
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
            raise PayloadError("select item must have column, aggregate, temporal, rank, or *")

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
    computed += windowed
    joined = len(frames) > 1
    if aggregates:
        if computed:
            raise PayloadError("case/window columns cannot mix with aggregates")
        for column in plain:
            if column.get_name() not in seen:
                keys.append(column)
                seen.add(column.get_name())
        expr = expr.group_by(keys).aggregate(aggregates) if keys else expr.aggregate(aggregates)
    elif plain:
        expr = expr.select([*plain, *computed])
    elif joined:
        expr = expr.select([*_star_columns(frames), *computed])  # empty select -> deduped star
    elif computed:
        expr = expr.mutate(computed)
    # else: empty select on a single table -> all columns unchanged

    # Set operations run over the projection above, so a trailing `\order` and
    # `\limit` apply to the merged result (SQL's own reading of the document).
    expr = _apply_set_ops(con, expr, payload.get("setOps"), cte_tables)

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


def _build_ctes(con, ctes: object, tables: dict[str, ir.Table]) -> None:
    """Build each `ctes` entry in order into `tables`, so a later body can name
    an earlier CTE. Bodies build against the same connection through `build`;
    the map is threaded into table resolution (no `con` state mutation)."""
    if ctes is None:
        return
    if not isinstance(ctes, list):
        raise PayloadError("ctes must be an array")
    for spec in ctes:
        if not isinstance(spec, dict):
            raise PayloadError("cte specs must be objects")
        name = spec.get("name")
        if not isinstance(name, str) or not name:
            raise PayloadError("cte must have a non-empty name")
        if name in tables:
            raise PayloadError(f'duplicate cte name "{name}"')
        body = spec.get("body")
        if not isinstance(body, dict):
            raise PayloadError(f"cte {name!r} must have a body object")
        if body.get("ctes"):
            raise PayloadError("nested CTEs are not supported")
        tables[name] = build(con, body, tables)


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
