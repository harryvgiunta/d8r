"""Set operations (`\\union` / `\\intersect` / `\\except`): compilation + execution.

The operands are CTEs over the bundled `events` table, so every expectation here
is a fact about the demo data that can be checked by hand: 100 events cycling
four event types (25 clicks, 25 purchases) over 25 users, four events each.
"""

from __future__ import annotations

import json
from pathlib import Path

import ibis
import pytest

from anyql.engine import expression
from anyql.engine.execute import execute
from anyql.query import parse_query, payload_from_ast

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

CLICKS_CTE = {
    "name": "clicks",
    "body": {
        "dataset": "events",
        "select": [{"column": "timestamp"}, {"column": "user_id"}, {"column": "amount"}],
        "where": {"column": "event_type", "op": "=", "value": "click"},
    },
}

PURCHASES_CTE = {
    "name": "purchases",
    "body": {
        "dataset": "events",
        "select": [{"column": "timestamp"}, {"column": "user_id"}, {"column": "amount"}],
        "where": {"column": "event_type", "op": "=", "value": "purchase"},
    },
}

# `\from clicks \select timestamp user_id amount \union all purchases`.
MERGED_PAYLOAD = {
    "dataset": "clicks",
    "ctes": [CLICKS_CTE, PURCHASES_CTE],
    "select": [{"column": "timestamp"}, {"column": "user_id"}, {"column": "amount"}],
    "setOps": [{"op": "union", "dataset": "purchases", "distinct": False}],
}

SETOP_PAYLOAD = {
    **MERGED_PAYLOAD,
    "orderBy": [{"target": "timestamp", "direction": "asc"}],
    "limit": 10,
}

# The same query written as a document: two CTE blocks, a merge, an order, a cap.
SETOP_DOC = """\\with clicks
  \\from events
  \\select timestamp, user_id, amount
  \\where event_type = "click"
\\with purchases
  \\from events
  \\select timestamp, user_id, amount
  \\where event_type = "purchase"
\\from clicks
\\select timestamp, user_id, amount
\\union all purchases
\\order timestamp
\\limit 10"""


def load(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def test_union_all_payload_executes_matching_fixture(con):
    result = execute(con, SETOP_PAYLOAD)
    fixture = load("expected_setop_rows.json")
    assert result["columns"] == fixture["columns"]
    assert result["rows"] == fixture["rows"]


def test_the_document_path_builds_the_same_result(con):
    """The clause language reaches the engine: parse -> payload -> rows."""
    ast = parse_query(SETOP_DOC)
    assert ast.errors == []
    payload = payload_from_ast(ast)
    assert payload["setOps"] == [{"op": "union", "dataset": "purchases", "distinct": False}]
    assert execute(con, payload)["rows"] == load("expected_setop_rows.json")["rows"]


def test_union_all_keeps_duplicates_and_union_dedupes(con):
    # Both sides are the same 25 click events.
    all_rows = execute(
        con, {**MERGED_PAYLOAD, "setOps": [{"op": "union", "dataset": "clicks", "distinct": False}]}
    )
    distinct_rows = execute(
        con, {**MERGED_PAYLOAD, "setOps": [{"op": "union", "dataset": "clicks", "distinct": True}]}
    )
    assert len(all_rows["rows"]) == 50
    assert len(distinct_rows["rows"]) == 25


def test_intersect_all_keeps_multiplicity_where_the_distinct_form_collapses(con):
    # Every user appears 4x in events; doubling it makes 8x on the left, and
    # INTERSECT ALL keeps the smaller multiplicity (4), where INTERSECT keeps one.
    doubled = {
        "dataset": "events",
        "select": [{"column": "user_id"}],
        "setOps": [{"op": "union", "dataset": "events", "distinct": False}],
    }
    assert len(execute(con, doubled)["rows"]) == 200

    kept = execute(
        con,
        {
            **doubled,
            "setOps": [
                *doubled["setOps"],
                {"op": "intersect", "dataset": "events", "distinct": False},
            ],
        },
    )
    collapsed = execute(
        con,
        {
            **doubled,
            "setOps": [
                *doubled["setOps"],
                {"op": "intersect", "dataset": "events", "distinct": True},
            ],
        },
    )
    assert len(kept["rows"]) == 100
    assert len(collapsed["rows"]) == 25


def test_except_removes_the_operands_rows(con):
    payload = {
        "dataset": "users",
        "select": [{"column": "user_id"}],
        "ctes": [
            {
                "name": "apac",
                "body": {
                    "dataset": "users",
                    "select": [{"column": "user_id"}],
                    "where": {"column": "region", "op": "=", "value": "apac"},
                },
            }
        ],
        "setOps": [{"op": "except", "dataset": "apac", "distinct": True}],
        "orderBy": [{"target": "user_id", "direction": "asc"}],
    }
    rows = execute(con, payload)["rows"]
    assert len(rows) == 17  # 25 users minus the 8 apac ones
    removed = sorted(set(range(1, 26)) - {row[0] for row in rows})
    assert removed == [3, 6, 9, 12, 15, 18, 21, 24]


def test_the_operand_is_projected_to_the_lefts_columns(con):
    # `events` carries five columns; the one-column projection is what the
    # operation is over, so the operand is narrowed to it (extra columns drop).
    payload = {
        "dataset": "events",
        "select": [{"column": "user_id"}],
        "setOps": [{"op": "union", "dataset": "users", "distinct": False}],
    }
    assert len(execute(con, payload)["rows"]) == 125

    # A column the operand does not have is the user-level error, not ibis' own.
    with pytest.raises(expression.PayloadError, match='is missing region'):
        expression.build(
            con,
            {
                "dataset": "users",
                "select": [{"column": "user_id"}, {"column": "region"}],
                "setOps": [{"op": "union", "dataset": "events", "distinct": True}],
            },
        )


def test_order_and_limit_apply_to_the_merged_result(con):
    payload = {
        "dataset": "events",
        "select": [{"column": "user_id"}],
        "setOps": [{"op": "union", "dataset": "users", "distinct": False}],
        "orderBy": [{"target": "user_id", "direction": "desc"}],
        "limit": 4,
    }
    # 4 events + 1 users row carry user 25, and the ordering is over both sides.
    assert execute(con, payload)["rows"] == [[25], [25], [25], [25]]
    sql = expression.compile_sql(expression.build(con, payload))
    assert sql.index("UNION ALL") < sql.index("ORDER BY")


def test_set_op_sql_reads_as_the_operation_it_is(con):
    def sql(op: str) -> str:
        return expression.compile_sql(
            expression.build(
                con,
                {
                    "dataset": "events",
                    "select": [{"column": "user_id"}],
                    "setOps": [{"op": op, "dataset": "events", "distinct": True}],
                },
            )
        )

    assert "UNION" in sql("union")
    assert "INTERSECT" in sql("intersect")
    assert "EXCEPT" in sql("except")


def test_conflicting_column_types_surface_as_a_payload_error():
    # No bundled pair of datasets shares a column name across two dtypes, so the
    # conflict is built where it can exist: two tables, one name, two types.
    con = ibis.duckdb.connect()
    con.create_table("ints", ibis.memtable({"k": [1, 2]}))
    con.create_table("texts", ibis.memtable({"k": ["a"]}))
    with pytest.raises(expression.PayloadError, match=r'\\union "texts": Table schemas must be equal'):
        expression.build(
            con,
            {
                "dataset": "ints",
                "select": [{"column": "k"}],
                "setOps": [{"op": "union", "dataset": "texts", "distinct": True}],
            },
        )


def test_malformed_set_op_payload_entries_are_payload_errors(con):
    base = {"dataset": "events", "select": [{"column": "user_id"}]}
    with pytest.raises(expression.PayloadError, match="unknown set operation: 'minus'"):
        expression.build(con, {**base, "setOps": [{"op": "minus", "dataset": "users"}]})
    with pytest.raises(expression.PayloadError, match="unknown dataset: ghost"):
        expression.build(con, {**base, "setOps": [{"op": "union", "dataset": "ghost"}]})
    with pytest.raises(expression.PayloadError, match="distinct must be a boolean"):
        expression.build(
            con, {**base, "setOps": [{"op": "union", "dataset": "users", "distinct": "yes"}]}
        )
    with pytest.raises(expression.PayloadError, match="setOps must be an array"):
        expression.build(con, {**base, "setOps": {"op": "union"}})
    with pytest.raises(expression.PayloadError, match="setOp specs must be objects"):
        expression.build(con, {**base, "setOps": ["union"]})
