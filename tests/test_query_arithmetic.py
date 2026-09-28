"""Arithmetic parsing: precedence, safe boundaries, and expression integration."""

import pytest

from d8r.query import ColumnDef, FnDef, SchemaContext, TableDef, parse_body, parse_query, payload_from_ast
from d8r.query.functions import MAX_EXPRESSION_DEPTH, MAX_EXPRESSION_NODES
from d8r.query.parser import parse_scalar_call


def selected(expression):
    ast = parse_query(f"\\from orders\n\\select {expression}", settled=True)
    assert ast.errors == []
    return payload_from_ast(ast)["select"][0]


def test_precedence_grouping_and_left_associativity():
    assert selected("a - b - c * (d + 2)")["arithmetic"] == {
        "op": "-",
        "args": [
            {"op": "-", "args": [{"column": "a"}, {"column": "b"}]},
            {"op": "*", "args": [
                {"column": "c"},
                {"op": "+", "args": [{"column": "d"}, {"literal": {"value": 2}}]},
            ]},
        ],
    }
    assert selected("a / b / c")["arithmetic"] == {
        "op": "/", "args": [
            {"op": "/", "args": [{"column": "a"}, {"column": "b"}]}, {"column": "c"},
        ],
    }


def test_signed_scientific_literals_and_unary_grouping():
    assert selected("-2.5e-3")["literal"] == {"value": -0.0025}
    assert selected("1e-3 + -2E+2")["arithmetic"] == {
        "op": "+", "args": [{"literal": {"value": 0.001}}, {"literal": {"value": -200.0}}],
    }
    assert selected("-(amount + +other)")["arithmetic"] == {
        "op": "-", "args": [{"op": "+", "args": [
            {"column": "amount"}, {"op": "+", "args": [{"column": "other"}]},
        ]}],
    }
    assert selected("(amount)")["column"] == "amount"
    assert selected("('a/b')")["literal"] == {"value": "a/b"}


def test_aggregate_leaves_scalar_arguments_and_output_aliases():
    divided = {"op": "/", "args": [{"column": "amount"}, {"literal": {"value": 100}}]}
    assert selected("coalesce(amount/100,0)")["scalar"] == {
        "fn": "coalesce", "args": [divided, {"literal": {"value": 0}}],
    }
    item = selected("sum(amount)/100 as total_dollars")
    assert item["arithmetic"] == {
        "op": "/", "args": [{"aggregate": {"fn": "sum", "arg": "amount"}}, {"literal": {"value": 100}}],
    }
    assert item["alias"] == "total_dollars"
    assert selected("count(*)/2")["arithmetic"]["args"][0] == {"aggregate": {"fn": "count", "arg": "*"}}
    assert selected("amount/100")["alias"] == "amount/100"
    ast = parse_query("\\from orders\n\\select amount/100 as dollars\n\\order dollars", settled=True)
    assert ast.errors == []
    assert ast.order_by[0].resolves_to == 1


def test_quoted_operators_inside_scalar_calls_are_not_grammar():
    assert selected("length('a''+b/c') / 2")["arithmetic"] == {
        "op": "/", "args": [
            {"fn": "length", "args": [{"literal": {"value": "a'+b/c"}}]},
            {"literal": {"value": 2}},
        ],
    }


@pytest.mark.parametrize("expression", [
    "amount/", "amount ** 2", "(amount + 2", "amount + )", "amount % 2",
    "sum(amount/100)", "coalesce(sum(amount)/100,0)", "upper((sum(amount)))",
    "count(*) over (order by amount) / 2", "sum(amount)/100 over (order by amount)",
    "(\\from orders \\select amount) / 2", "2 + (\\from orders \\select amount)",
    "year(placed_at) / 2", "true + 1", "'2' * 1", "1e309 + 1",
])
def test_unsupported_or_malformed_arithmetic_is_a_parser_error(expression):
    ast = parse_query(f"\\from orders\n\\select {expression}", settled=True)
    assert ast.errors
    assert {error.line for error in ast.errors} == {2}


def test_grouping_and_scalar_depth_limits_fail_before_python_recursion():
    assert selected("(" * (MAX_EXPRESSION_DEPTH - 1) + "1" + ")" * (MAX_EXPRESSION_DEPTH - 1))["literal"] == {"value": 1}
    for expression in (
        "(" * MAX_EXPRESSION_DEPTH + "1" + ")" * MAX_EXPRESSION_DEPTH,
        "+ " * MAX_EXPRESSION_DEPTH + "amount",
        " + ".join(["amount"] * (MAX_EXPRESSION_DEPTH + 1)),
        "coalesce(" * MAX_EXPRESSION_DEPTH + "amount" + ",0)" * MAX_EXPRESSION_DEPTH,
    ):
        ast = parse_query(f"\\from orders\n\\select {expression}", settled=True)
        assert ast.errors
    with pytest.raises(ValueError):
        parse_scalar_call("coalesce(" * 1000 + "amount" + ",0)" * 1000)


def test_expression_node_budget_bounds_wide_scalar_arguments():
    values = ",".join(["0"] * (MAX_EXPRESSION_NODES - 1))
    assert selected(f"coalesce({values})")["scalar"]["args"] == [{"literal": {"value": 0}}] * (MAX_EXPRESSION_NODES - 1)
    ast = parse_query(f"\\from orders\n\\select coalesce({values},0)", settled=True)
    assert ast.errors


def test_nested_arithmetic_qualified_references_obey_table_aliases():
    schema = SchemaContext(tables=(TableDef("orders", "orders", (ColumnDef("amount", "int64", ""),)),))
    ast = parse_query("\\from orders o\n\\select coalesce(o.amount/100,0) + sum(o.amount)", schema=schema, settled=True)
    assert ast.errors == []
    ast = parse_query("\\from orders o\n\\select coalesce(orders.amount/100,0) + sum(missing.amount)", schema=schema, settled=True)
    assert len(ast.errors) == 2
    assert all(error.line == 2 for error in ast.errors)


def test_function_numeric_parameters_expand_inside_arithmetic():
    body = "\\from orders\n\\select coalesce(amount/@scale,0) as dollars"
    assert parse_body(body, ["scale"]) == []
    schema = SchemaContext(fns=(FnDef("scaled", ("scale",), body),))
    ast = parse_query("\\from scaled(1e+2)", schema=schema, settled=True)
    assert ast.errors == []
    assert payload_from_ast(ast)["body"]["select"][0]["scalar"]["args"][0] == {
        "op": "/", "args": [{"column": "amount"}, {"literal": {"value": 100.0}}],
    }


def test_query_subqueries_keep_their_separate_parser():
    item = selected("(\\from orders \\select max(amount)) as peak")
    assert item["arithmetic"] is None
    assert item["subquery"]["select"][0]["aggregate"] == {"fn": "max", "arg": "amount"}
    assert item["alias"] == "peak"
