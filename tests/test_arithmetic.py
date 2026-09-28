"""Numeric projections and reductions through real Ibis SQLite/DuckDB backends."""

from __future__ import annotations

import ibis
import pytest

from d8r.engine import expression
from d8r.engine.execute import execute
from d8r.query import parse_query, payload_from_ast
from d8r.query.functions import MAX_EXPRESSION_DEPTH, MAX_EXPRESSION_NODES


@pytest.fixture(scope="module", params=["duckdb", "sqlite"])
def arithmetic_con(request):
    con = getattr(ibis, request.param).connect()
    data = ibis.memtable(
        [
            (1, "north", 199, 100, "abc", True),
            (2, "north", 101, 0, "x", False),
            (3, "south", 250, None, "zz", True),
            (4, "south", 199, 100, "", True),
            (5, "north", None, 100, None, None),
        ],
        schema={
            "id": "int64", "region": "string", "amount_cents": "int64",
            "divisor": "int64", "label": "string", "active": "boolean",
        },
    )
    con.create_table("payments", data)
    try:
        yield con
    finally:
        con.disconnect()


def run(con, select: str, clauses: str = "") -> dict:
    ast = parse_query(f"\\from payments\n\\select {select}\n{clauses}\n\\limit 100")
    assert ast.errors == []
    return execute(con, payload_from_ast(ast))


def literal(value):
    return {"literal": {"value": value}}


def arithmetic(op, *args):
    return {"op": op, "args": list(args)}


def aggregate(fn="sum", arg="amount_cents"):
    return {"aggregate": {"fn": fn, "arg": arg}}


def build_raw(con, node, **extra):
    return expression.build(
        con, {"dataset": "payments", "select": [{"arithmetic": node, "alias": "value", **extra}]}
    )


def test_integer_cents_division_is_fractional_and_keeps_alias(arithmetic_con):
    result = run(
        arithmetic_con,
        "amount_cents / 100 as total_dollars, amount_cents / 100",
        "\\where id = 1",
    )
    assert result["columns"] == ["total_dollars", "amount_cents / 100"]
    assert result["rows"] == [[1.99, 1.99]]


def test_precedence_left_associativity_parentheses_and_unary(arithmetic_con):
    result = run(
        arithmetic_con,
        "8 / 4 / 2 as divided, 2 + 3 * 4 as priority, (2 + 3) * 4 as grouped, "
        "2 * -3 as negative, -(amount_cents + 1) / 100 as dollars, "
        "+amount_cents as positive, amount_cents - 100 - 1 as subtracted",
        "\\where id = 1",
    )
    assert result["rows"] == [[1.0, 14, 20, -6, -2.0, 199, 98]]


def test_filtered_aggregate_math_and_aggregate_only_ratio(arithmetic_con):
    result = run(
        arithmetic_con,
        "sum(amount_cents) / 100 as total_dollars, "
        "sum(amount_cents) / count(*) as average_cents, count(*) / 2 as pairs",
        "\\where id < 4",
    )
    assert result["rows"][0] == pytest.approx([5.5, 550 / 3, 1.5])


def test_grouped_aggregate_math_respects_null_counts(arithmetic_con):
    result = run(
        arithmetic_con,
        "region, sum(amount_cents) / 100 as total_dollars, "
        "sum(amount_cents) / count(amount_cents) as mean_cents",
        "\\group region\n\\order region",
    )
    assert [row[0] for row in result["rows"]] == ["north", "south"]
    assert result["rows"][0][1:] == pytest.approx([3.0, 150.0])
    assert result["rows"][1][1:] == pytest.approx([4.49, 224.5])


def test_row_math_groups_implicitly_and_constant_math_does_not(arithmetic_con):
    result = run(
        arithmetic_con,
        "amount_cents / 100 as dollars, count(*) / 2 as pairs, 1 / 2 as marker",
        "\\where amount_cents is not null\n\\order dollars",
    )
    assert result["rows"] == [[1.01, 0.5, 0.5], [1.99, 1.0, 0.5], [2.5, 0.5, 0.5]]


def test_scalar_calls_accept_row_math_and_keep_implicit_grouping(arithmetic_con):
    result = run(
        arithmetic_con,
        "string(amount_cents + 1) as label, count(*) as n, length('abcd') / 2 as marker",
        "\\where amount_cents is not null\n\\order label",
    )
    assert result["rows"] == [["102", 1, 2.0], ["200", 2, 2.0], ["251", 1, 2.0]]
    result = run(
        arithmetic_con,
        "length(upper(label)) + 1 as length, substr(label, 1 + 1) as suffix",
        "\\where id = 1",
    )
    assert result["rows"] == [[4, "c"]]


def test_empty_input_keeps_constants_out_of_grouping(arithmetic_con):
    assert run(arithmetic_con, "1 / 2 as marker", "\\where id < 0")["rows"] == []
    result = run(
        arithmetic_con,
        "count(*) / 2 as pairs, sum(amount_cents) / 100 as dollars, "
        "1 / 2 as marker, string(2 + 3) as label",
        "\\where id < 0",
    )
    assert result["rows"] == [[0.0, None, 0.5, "5"]]


def test_null_and_zero_division_propagate_null(arithmetic_con):
    result = run(arithmetic_con, "id, amount_cents / divisor as value", "\\order id")
    assert result["rows"] == [[1, 1.99], [2, None], [3, None], [4, 1.99], [5, None]]
    result = run(
        arithmetic_con,
        "1 / 0 as zero, 0 / 0 as both_zero, 1 / null as denominator, "
        "null / 2 as numerator, null + 1 as addition, -null as negative, "
        "count(*) / (count(*) - count(*)) as aggregate_zero",
    )
    assert result["rows"] == [[None] * 7]


@pytest.mark.parametrize("operand", [
    literal("199"), literal(True), {"column": "label"}, {"column": "active"},
    {"fn": "upper", "args": [{"column": "label"}]},
    {"fn": "contains", "args": [{"column": "label"}, literal("a")]},
    aggregate("min", "label"),
])
def test_arithmetic_rejects_non_numeric_operands(arithmetic_con, operand):
    with pytest.raises(expression.PayloadError, match="numeric"):
        build_raw(arithmetic_con, arithmetic("/", operand, literal(100)))


def test_numeric_aggregate_outputs_can_count_text(arithmetic_con):
    result = run(arithmetic_con, "count(label) / 2 as pairs")
    assert result["rows"] == [[2.0]]


@pytest.mark.parametrize("node", [
    arithmetic("+", aggregate(), {"column": "amount_cents"}),
    arithmetic("/", aggregate(), {"fn": "length", "args": [{"column": "label"}]}),
    arithmetic("+", literal(1), arithmetic("*", aggregate(), {"column": "id"})),
])
def test_one_expression_cannot_mix_aggregates_and_row_dependencies(arithmetic_con, node):
    with pytest.raises(expression.PayloadError, match="CTE"):
        build_raw(arithmetic_con, node)


def test_scalar_calls_reject_aggregates_at_every_nested_level(arithmetic_con):
    scalar = {"fn": "string", "args": [arithmetic("/", aggregate(), literal(100))]}
    with pytest.raises(expression.PayloadError, match="inside scalar"):
        expression.build(arithmetic_con, {"dataset": "payments", "select": [{"scalar": scalar}]})
    nested = arithmetic("+", literal(1), {"fn": "length", "args": [scalar]})
    with pytest.raises(expression.PayloadError, match="inside scalar"):
        build_raw(arithmetic_con, nested)


@pytest.mark.parametrize("item", [
    {"star": True},
    {"rank": {"fn": "row_number"}, "window": {"order": {"column": "id"}}},
    {"subquery": {"dataset": "payments", "select": [aggregate("max", "id")]}, "alias": "peak"},
])
def test_nested_aggregates_obey_existing_projection_mix_rules(arithmetic_con, item):
    with pytest.raises(expression.PayloadError, match="cannot mix"):
        expression.build(arithmetic_con, {
            "dataset": "payments",
            "select": [{"arithmetic": arithmetic("/", aggregate(), literal(100)), "alias": "dollars"}, item],
        })


@pytest.mark.parametrize("extra", [
    {"column": "id"}, {"literal": {"value": 1}},
    {"scalar": {"fn": "length", "args": [literal("a")]}},
    {"star": True}, {"window": {"order": {"column": "id"}}},
])
def test_arithmetic_select_cannot_hide_another_expression(arithmetic_con, extra):
    with pytest.raises(expression.PayloadError):
        build_raw(arithmetic_con, arithmetic("+", literal(1), literal(2)), **extra)


@pytest.mark.parametrize("node", [
    None, [], {"column": "id"},
    {"op": "+", "args": [literal(1), literal(2)], "extra": True},
    arithmetic("**", literal(2), literal(3)), arithmetic([], literal(1)),
    arithmetic("*", literal(1)), arithmetic("+"), arithmetic("-", literal(1), literal(2), literal(3)),
    {"op": "/", "args": {"column": "id"}},
    arithmetic("+", "id", literal(1)),
    arithmetic("+", {"column": "id", "literal": {"value": 1}}, literal(1)),
    arithmetic("+", {"literal": {"value": 1, "extra": True}}, literal(1)),
    arithmetic("+", literal([]), literal(1)),
    arithmetic("+", {"aggregate": {"fn": [], "arg": "id"}}, literal(1)),
    arithmetic("+", {"aggregate": {"fn": "sum", "arg": {"op": "+", "args": []}}}, literal(1)),
    arithmetic("+", {"aggregate": {"fn": "sum", "arg": "id", "window": {}}}, literal(1)),
    arithmetic("+", aggregate("sum", "*"), literal(1)),
    arithmetic("+", {"subquery": {"dataset": "payments"}}, literal(1)),
    arithmetic("+", {"fn": "length", "args": [literal("a")], "window": {}}, literal(1)),
])
def test_malformed_raw_arithmetic_is_a_payload_error(arithmetic_con, node):
    with pytest.raises(expression.PayloadError):
        build_raw(arithmetic_con, node)


def test_raw_payload_depth_and_node_bounds_include_scalar_calls(arithmetic_con):
    node = literal(1)
    for _ in range(MAX_EXPRESSION_DEPTH + 1):
        node = arithmetic("+", node)
    with pytest.raises(expression.PayloadError, match="depth"):
        build_raw(arithmetic_con, node)

    node = literal("x")
    for _ in range(MAX_EXPRESSION_DEPTH + 1):
        node = {"fn": "upper", "args": [node]}
    with pytest.raises(expression.PayloadError, match="depth"):
        expression.build(arithmetic_con, {"dataset": "payments", "select": [{"scalar": node}]})

    wide = {"fn": "coalesce", "args": [literal(1)] * MAX_EXPRESSION_NODES}
    with pytest.raises(expression.PayloadError, match="nodes"):
        build_raw(arithmetic_con, arithmetic("+", wide, literal(1)))

    cyclic = arithmetic("+")
    cyclic["args"].append(cyclic)
    with pytest.raises(expression.PayloadError, match="depth"):
        build_raw(arithmetic_con, cyclic)


