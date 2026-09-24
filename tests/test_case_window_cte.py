"""Case / window / CTE clauses: compilation + execution against fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from anyql.engine import expression
from anyql.engine.execute import execute

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

CASE_PAYLOAD = {
    "dataset": "events",
    "select": [{"column": "timestamp"}, {"column": "event_type"}, {"column": "amount"}],
    "cases": [
        {
            "alias": "flag",
            "whens": [{"column": "event_type", "op": "=", "value": "purchase", "then": "1"}],
            "else": "0",
        }
    ],
    "orderBy": [{"target": "timestamp", "direction": "asc"}],
    "limit": 8,
}

WINDOW_PAYLOAD = {
    "dataset": "events",
    "select": [
        {"column": "timestamp"},
        {"column": "user_id"},
        {"column": "amount"},
        {
            "aggregate": {"fn": "avg", "arg": "amount"},
            "window": {"partitionBy": ["user_id"], "order": None},
            "alias": "amount_avg",
        },
    ],
    "orderBy": [{"target": "timestamp", "direction": "asc"}],
    "limit": 8,
}

CTE_PAYLOAD = {
    "dataset": "reg",
    "ctes": [
        {
            "name": "reg",
            "body": {
                "dataset": "users",
                "select": [{"column": "region"}],
                "where": {"column": "region", "op": "=", "value": "us"},
            },
        }
    ],
    "select": [{"column": "region"}],
    "orderBy": [{"target": "region", "direction": "asc"}],
}

CTE_CHAIN_PAYLOAD = {
    "dataset": "big",
    "ctes": [
        {
            "name": "reg",
            "body": {
                "dataset": "users",
                "select": [{"column": "user_id"}, {"column": "region"}],
                "where": {"column": "region", "op": "=", "value": "us"},
            },
        },
        {
            "name": "big",
            "body": {"dataset": "reg", "select": [{"column": "user_id"}], "limit": 3},
        },
    ],
    "select": [{"column": "user_id"}],
    "orderBy": [{"target": "user_id", "direction": "asc"}],
}


def load(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def test_case_payload_executes_matching_fixture(con):
    result = execute(con, CASE_PAYLOAD)
    fixture = load("expected_case_rows.json")
    assert result["columns"] == fixture["columns"]
    assert result["rows"] == fixture["rows"]


def test_case_compiles_to_case_sql(con):
    sql = expression.compile_sql(expression.build(con, CASE_PAYLOAD))
    assert "CASE WHEN" in sql
    # `else` coerced from "0" to the integer literal 0
    assert "ELSE 0 END" in sql


def test_case_without_else_defaults_to_null(con):
    payload = {
        "dataset": "events",
        "select": [{"column": "timestamp"}, {"column": "event_type"}],
        "cases": [
            {"alias": "lab", "whens": [{"column": "event_type", "op": "=", "value": "purchase", "then": "hit"}]}
        ],
        "orderBy": [{"target": "timestamp", "direction": "asc"}],
        "limit": 4,
    }
    rows = execute(con, payload)["rows"]
    assert [row[2] for row in rows] == [None, None, "hit", None]


def test_case_like_operator_in_when(con):
    payload = {
        "dataset": "events",
        "select": [{"column": "path"}],
        "cases": [
            {"alias": "p5", "whens": [{"column": "path", "op": "like", "value": "%/5", "then": "1"}], "else": "0"}
        ],
    }
    frame = expression.build(con, payload).execute()
    assert int(frame["p5"].sum()) == int((frame["path"] == "/p/5").sum())


def test_window_payload_executes_matching_fixture(con):
    result = execute(con, WINDOW_PAYLOAD)
    fixture = load("expected_window_rows.json")
    assert result["columns"] == fixture["columns"]
    assert result["rows"] == fixture["rows"]


def test_window_compiles_with_over_clause(con):
    sql = expression.compile_sql(expression.build(con, WINDOW_PAYLOAD))
    assert "OVER" in sql
    assert "PARTITION BY" in sql


def test_window_order_by_desc_adds_order_clause(con):
    payload = {
        **WINDOW_PAYLOAD,
        "select": [
            *WINDOW_PAYLOAD["select"][:3],
            {
                "aggregate": {"fn": "avg", "arg": "amount"},
                "window": {
                    "partitionBy": ["user_id"],
                    "order": {"column": "timestamp", "direction": "desc"},
                },
                "alias": "amount_avg",
            },
        ],
    }
    sql = expression.compile_sql(expression.build(con, payload))
    assert "ORDER BY" in sql.split("OVER", 1)[1]


def test_window_order_is_row_invariant_for_full_partition_avg(con):
    # avg over the whole partition: with or without an order key every row of a
    # user carries that user's mean, so the ordered form matches the plain one.
    ordered = execute(
        con,
        {
            **WINDOW_PAYLOAD,
            "select": [
                *WINDOW_PAYLOAD["select"][:3],
                {
                    "aggregate": {"fn": "avg", "arg": "amount"},
                    "window": {
                        "partitionBy": ["user_id"],
                        "order": {"column": "timestamp", "direction": "asc"},
                    },
                    "alias": "amount_avg",
                },
            ],
        },
    )
    plain = execute(con, WINDOW_PAYLOAD)
    assert ordered["rows"] == plain["rows"]


def test_window_over_empty_select_appends_after_star(con):
    payload = {
        "dataset": "events",
        "select": [
            {
                "aggregate": {"fn": "count", "arg": "event_type"},
                "window": {"partitionBy": ["event_type"], "order": None},
                "alias": "cnt",
            }
        ],
        "limit": 2,
    }
    frame = expression.build(con, payload).execute()
    assert list(frame.columns) == ["timestamp", "user_id", "event_type", "amount", "path", "cnt"]
    assert set(frame["cnt"]) == {25}  # 100 events / 4 types


def test_rank_is_one_based_and_dense(con):
    result = execute(
        con,
        {
            "dataset": "users",
            "select": [
                {"column": "user_id"},
                {"rank": {"fn": "rank"}, "window": {"partitionBy": [], "order": {"column": "score", "direction": "desc"}}, "alias": "r"},
                {"rank": {"fn": "dense_rank"}, "window": {"partitionBy": [], "order": {"column": "score", "direction": "desc"}}, "alias": "d"},
                {"rank": {"fn": "row_number"}, "window": {"partitionBy": [], "order": {"column": "score", "direction": "desc"}}, "alias": "n"},
            ],
            "orderBy": [{"target": "n", "direction": "asc"}],
        },
    )
    ranks = [row[1] for row in result["rows"]]
    assert min(ranks) >= 1  # 1-based despite ibis' 0-based dialect rendering
    assert ranks[0] == 1
    numbers = [row[3] for row in result["rows"]]
    assert numbers == list(range(1, len(result["rows"]) + 1))  # row_number is a permutation
    dense = [row[2] for row in result["rows"]]
    assert dense[0] == 1
    assert max(dense) <= max(ranks)  # dense ties collapse below plain rank


def test_rank_requires_over(con):
    with pytest.raises(expression.PayloadError, match="requires over"):
        expression.build(
            con,
            {"dataset": "events", "select": [{"rank": {"fn": "rank"}, "alias": "r"}]},
        )
    with pytest.raises(expression.PayloadError, match="over .* needs an aggregate or rank"):
        expression.build(
            con,
            {
                "dataset": "events",
                "select": [
                    {"column": "user_id", "window": {"partitionBy": ["user_id"], "order": None}, "alias": "w"}
                ],
            },
        )


def test_cte_payload_executes_matching_fixture(con):
    result = execute(con, CTE_PAYLOAD)
    fixture = load("expected_cte_rows.json")
    assert result["columns"] == fixture["columns"]
    assert result["rows"] == fixture["rows"]


def test_cte_can_be_joined_like_a_table(con):
    payload = {
        "dataset": "reg",
        "joins": [{"dataset": "events", "left": "user_id", "right": "user_id"}],
        "select": [{"column": "region"}, {"column": "amount"}],
        "ctes": [
            {
                "name": "reg",
                "body": {
                    "dataset": "users",
                    "select": [{"column": "user_id"}, {"column": "region"}],
                    "where": {"column": "region", "op": "=", "value": "apac"},
                },
            }
        ],
    }
    frame = expression.build(con, payload).execute()
    assert set(frame["region"]) == {"apac"}
    # inner-join conservation: exactly the events whose user is in the CTE
    expected = expression.build(
        con,
        {
            "dataset": "events",
            "joins": [{"dataset": "users", "left": "user_id", "right": "user_id"}],
            "select": [{"column": "amount"}],
            "where": {"column": "region", "op": "=", "value": "apac"},
        },
    ).execute()
    assert frame.shape[0] == expected.shape[0]


def test_cte_chain_references_earlier_cte(con):
    result = execute(con, CTE_CHAIN_PAYLOAD)
    fixture = load("expected_cte_chain_rows.json")
    assert result["columns"] == fixture["columns"]
    assert result["rows"] == fixture["rows"]


def test_cte_name_shadows_registered_dataset(con):
    payload = {
        "dataset": "users",
        "select": [{"column": "region"}],
        "ctes": [
            {
                "name": "users",
                "body": {
                    "dataset": "users",
                    "select": [{"column": "region"}],
                    "where": {"column": "region", "op": "=", "value": "apac"},
                    "limit": 2,
                },
            }
        ],
        "limit": 2,
    }
    frame = expression.build(con, payload).execute()
    assert set(frame["region"]) == {"apac"}


def test_cte_does_not_mutate_connection(con):
    expression.build(con, CTE_PAYLOAD)
    assert "reg" not in con.list_tables()


def test_payload_errors_case_window_cte(con):
    with pytest.raises(expression.PayloadError, match="case/window columns cannot mix with aggregates"):
        expression.build(
            con,
            {
                "dataset": "events",
                "select": [{"aggregate": {"fn": "sum", "arg": "amount"}, "alias": "total"}],
                "cases": [
                    {"alias": "f", "whens": [{"column": "event_type", "op": "=", "value": "x", "then": "1"}]}
                ],
            },
        )
    with pytest.raises(expression.PayloadError, match="windowed select items cannot mix with aggregates"):
        expression.build(
            con,
            {
                "dataset": "events",
                "select": [
                    {"aggregate": {"fn": "sum", "arg": "amount"}, "alias": "total"},
                    {
                        "aggregate": {"fn": "avg", "arg": "amount"},
                        "window": {"partitionBy": ["user_id"], "order": None},
                        "alias": "w",
                    },
                ],
            },
        )
    with pytest.raises(expression.PayloadError, match="window needs partitionBy and/or order"):
        expression.build(
            con,
            {
                "dataset": "events",
                "select": [
                    {
                        "aggregate": {"fn": "median", "arg": "amount"},
                        "window": {"partitionBy": [], "order": None},
                        "alias": "w",
                    }
                ],
            },
        )
    with pytest.raises(expression.PayloadError, match="unknown rank function"):
        expression.build(
            con,
            {
                "dataset": "events",
                "select": [
                    {"rank": {"fn": "median"}, "window": {"partitionBy": ["user_id"], "order": None}, "alias": "w"}
                ],
            },
        )
    with pytest.raises(expression.PayloadError, match="bogus"):
        expression.build(
            con,
            {
                "dataset": "events",
                "select": [
                    {
                        "aggregate": {"fn": "avg", "arg": "bogus"},
                        "window": {"partitionBy": ["user_id"], "order": None},
                        "alias": "w",
                    }
                ],
            },
        )
    with pytest.raises(expression.PayloadError, match="duplicate cte name"):
        expression.build(
            con,
            {
                "dataset": "users",
                "ctes": [
                    {"name": "a", "body": {"dataset": "users"}},
                    {"name": "a", "body": {"dataset": "users"}},
                ],
            },
        )
    with pytest.raises(expression.PayloadError, match="unknown dataset"):
        expression.build(
            con,
            {"dataset": "ghost", "ctes": [{"name": "a", "body": {"dataset": "users"}}]},
        )


def test_case_window_cte_payloads_default_empty(con):
    # Additive fields: payloads without them behave exactly as before.
    result = execute(con, {"dataset": "users", "limit": 2})
    assert result["columns"] == ["user_id", "region", "joined", "score"]
