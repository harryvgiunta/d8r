"""Filtered reductions through the document, payload and real Ibis backends."""

import ibis
import pytest

from d8r.engine.expression import PayloadError, build, compile_sql
from d8r.engine.execute import execute
from d8r.query import parse_query, payload_from_ast
from d8r.query.functions import MAX_WHERE_DEPTH, MAX_WHERE_NODES


@pytest.fixture(scope="module", params=["duckdb", "sqlite"])
def payments(request):
    con = getattr(ibis, request.param).connect()
    con.create_table("payments", ibis.memtable(
        [
            (1, "north", 100, "paid"),
            (2, "north", 30, "pending"),
            (3, "north", None, "paid"),
            (4, "south", 200, "paid"),
            (5, "south", 50, "pending"),
            (6, "west", 70, None),
        ],
        schema={"id": "int64", "region": "string", "amount": "float64", "status": "string"},
    ))
    con.create_table("regions", ibis.memtable(
        [("north", "keep"), ("south", "skip"), ("west", "keep")],
        schema={"region": "string", "status": "string"},
    ))
    try:
        yield con
    finally:
        con.disconnect()


def run(con, select, clauses="", *, source="payments"):
    ast = parse_query(f"\\from {source}\n\\select {select}\n{clauses}", settled=True)
    assert not ast.errors
    payload = payload_from_ast(ast)
    expr = build(con, payload)
    for dialect in ("duckdb", "postgres", "sqlite"):
        compile_sql(expr, dialect)
    return execute(con, payload)["rows"]


def test_grouped_filters_are_independent_and_keep_unmatched_groups(payments):
    rows = run(payments, r"""region,
        sum(amount \where status = 'paid') as paid,
        sum(amount \where status = 'pending') as pending,
        count(* \where status = 'paid') as paid_rows,
        count(amount \where status = 'paid') as paid_values,
        count(*) as all_rows""".replace("\n", " "), "\\group region\n\\order region")
    assert rows == [
        ["north", 100, 30, 2, 1, 3],
        ["south", 200, 50, 1, 1, 2],
        ["west", None, None, 0, 0, 1],
    ]


def test_predicate_precedence_groups_between_lists_and_null(payments):
    rows = run(payments, r"""
        sum(amount \where status = 'paid' or status = 'pending' and amount between 40 and 150) as loose,
        sum(amount \where (status = 'paid' or status = 'pending') and amount between 40 and 150) as grouped,
        count(* \where status not in ('paid', 'pending') or status is null) as other,
        count(* \where amount is null) as missing,
        count(* \where status like 'pa%') as matching""".replace("\n", " "))
    assert rows == [[350, 150, 1, 1, 3]]


def test_filtered_arithmetic_leaves_respect_outer_where(payments):
    rows = run(payments, r"""
        sum(amount \where status = 'paid') / count(amount \where status = 'paid') as mean,
        avg(amount \where status = 'paid') as average,
        min(amount \where status = 'paid') as minimum,
        max(amount \where status = 'paid') as maximum,
        count(* \where status = 'pending') + count(* \where status = 'paid') as known,
        count(*) as total""".replace("\n", " "), "\\where id < 5")
    assert rows == [[150.0, 150.0, 100, 200, 4, 4]]


def test_filtered_empty_global_reductions_keep_sql_null_semantics(payments):
    projection = r"count(* \where status = 'paid') as rows, sum(amount \where status = 'paid') as total"
    assert run(payments, projection, "\\where id < 0") == [[0, None]]
    assert run(payments, projection, "\\where region = 'west'") == [[0, None]]


def test_filtered_windows_preserve_every_input_row(payments):
    rows = run(payments, r"""id,
        sum(amount \where status = 'paid') over (partition by region) as paid,
        count(* \where status = 'paid') over (partition by region) as paid_rows
        """.replace("\n", " "), "\\order id")
    assert rows == [[1, 100, 2], [2, 100, 2], [3, 100, 2], [4, 200, 1], [5, 200, 1], [6, None, 0]]


@pytest.mark.parametrize(("projection", "rows"), [
    (
        r"count(* \where r.status = 'keep') as kept, count(* \where p.status = 'paid') as paid, count(*) as total",
        [[3, 3, 5]],
    ),
    (
        r"count(* \where r.status = 'keep') / count(*) as share",
        [[0.6]],
    ),
    (
        r"p.id, count(* \where p.status = 'paid') over (partition by r.status) as paid",
        [[1, 2], [3, 2], [4, 1], [5, 1], [6, 2]],
    ),
])
def test_filtered_counts_bind_to_joined_and_filtered_relation(payments, projection, rows):
    clauses = "\\join regions r on p.region = r.region\n\\where p.id != 2"
    if projection.startswith("p.id"):
        clauses += "\n\\order id"
    assert run(payments, projection, clauses, source="payments p") == rows


def test_predicate_subqueries_lower_inside_arithmetic_leaves(payments):
    rows = run(payments, r"count(* \where id in (\from payments \select id \where status = 'paid')) / 2 as pairs")
    assert rows == [[1.5]]


@pytest.mark.parametrize("where", [
    False, [], "status = 'paid'", {},
    {"column": "status", "op": []},
    {"column": "missing", "op": "=", "value": "paid"},
    {"column": "status", "op": "="},
    {"column": "status", "op": "=", "value": {"sql": "anything"}},
    {"column": "status", "op": "in", "values": []},
    {"column": "amount", "op": "between", "low": "1"},
    {"column": "status", "op": "is null", "value": "paid"},
    {"column": "status", "op": "=", "value": "paid", "ands": {}},
    {"column": "status", "op": "=", "value": "paid", "ors": [[]]},
    {"group": None},
    {"group": {"column": "status", "op": "is null"}, "column": "amount"},
    {"column": "status", "op": "=", "value": "paid", "extra": True},
    {"column": "status", "op": "=", "value": "paid", "ands": [
        {"column": "id", "op": ">", "value": 0, "ors": [[{"column": "id", "op": "<", "value": 10}]]}
    ]},
])
def test_malformed_raw_predicates_are_payload_errors(payments, where):
    with pytest.raises(PayloadError):
        build(payments, {"dataset": "payments", "select": [
            {"aggregate": {"fn": "count", "arg": "*", "where": where}, "alias": "n"}
        ]})


def test_raw_predicate_limits_reject_deep_wide_and_cyclic_trees(payments):
    leaf = {"column": "id", "op": ">", "value": 0}
    deep = leaf
    for _ in range(MAX_WHERE_DEPTH + 1):
        deep = {"group": deep}
    wide = {**leaf, "ands": [leaf] * MAX_WHERE_NODES}
    cycle = {}
    cycle["group"] = cycle
    for where in (deep, wide, cycle):
        with pytest.raises(PayloadError):
            build(payments, {"dataset": "payments", "select": [
                {"aggregate": {"fn": "sum", "arg": "amount", "where": where}}
            ]})


@pytest.mark.parametrize("predicate", ["count(*) > 2", "total_count > 2"])
def test_where_filters_aggregate_results_by_call_or_alias(payments, predicate):
    assert run(payments, "region, count(*) as total_count",
               f"\\where {predicate}\n\\order region") == [["north", 3]]


def test_where_only_reductions_do_not_leak_into_projection(payments):
    assert run(payments, "region", "\\where count(*) > 1\n\\order region") == [
        ["north"], ["south"],
    ]


def test_row_conjuncts_filter_before_all_reductions(payments):
    assert run(payments, "region, count(*) as n, sum(amount) as total",
               "\\where status = 'paid' and (n > 1 and sum(amount) >= 100)\n\\order region") == [
        ["north", 2, 100],
    ]
    assert run(payments, "p.region, count(*) as n, sum(p.amount) as total",
               "\\join regions r on p.region = r.region\n"
               "\\where p.id != 2 and n > 1 and sum(p.amount) >= 100\n\\order region",
               source="payments p") == [["north", 2, 100], ["south", 2, 250]]


def test_aggregate_or_keeps_group_keys_and_boolean_precedence(payments):
    assert run(payments, "region, count(*) as n",
               "\\where n > 2 or region = 'west'\n\\order region") == [["north", 3], ["west", 1]]
    assert run(payments, "region, count(*) as n",
               "\\where (n > 2 or region = 'west') and status = 'paid'\n\\order region") == []
    with pytest.raises(PayloadError, match="(?i)group|aggregate"):
        run(payments, "region, count(*) as n", "\\where n > 2 or status = 'pending'")


def test_aggregate_alias_precedence_keeps_qualified_source_escape(payments):
    assert run(payments, "region, count(*) as amount",
               "\\where amount > 2 and p.amount >= 0\n\\order region", source="payments p") == []


def test_aggregate_math_and_filtered_call_predicates(payments):
    assert run(payments, "region, sum(amount) / 100 as dollars",
               "\\where dollars > 2\n\\order region") == [["south", 2.5]]
    assert run(payments, "region, count(*) as n",
               "\\where count(* \\where status = 'paid') > 1\n\\order region") == [["north", 3]]


def test_empty_global_aggregate_filters_after_reduction(payments):
    assert run(payments, "count(*) as n", "\\where id < 0 and n = 0") == [[0]]
    assert run(payments, "count(*) as n", "\\where id < 0 and count(*) > 0") == []


def test_aggregate_predicate_operand_shapes_and_subquery_scope(payments):
    assert run(payments, "region, count(*) as n",
               "\\where count(*) between 2 and 3 and n in (2, 3) and sum(amount) is not null\n\\order region") == [
        ["north", 3], ["south", 2],
    ]
    assert run(payments, "region, count(*) as n",
               "\\where count(*) > (\\from regions \\select count(*) \\where status = 'keep')\n\\order region") == [
        ["north", 3],
    ]
