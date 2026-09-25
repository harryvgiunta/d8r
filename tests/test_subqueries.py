"""Subqueries: the inline `( … )` form, everywhere it may stand.

A subquery is the document's own grammar on one line, so the parser, the
validator, and the payload mapping are the ones the rest of the language uses —
these tests pin what that produces: a derived table in `\\from`/`\\join`, a
set-operation operand, an `in`/`not in` set, and a scalar value. The engine side
runs each shape against the real demo data and compares it with the same
computation written without a subquery, so "it executes" also means "the same
rows".
"""

from __future__ import annotations

import pandas as pd
import pytest

from d8r.engine import expression
from d8r.engine.execute import execute
from d8r.query import parse_query, payload_from_ast


def messages(ast) -> list[str]:
    return [error.message for error in ast.errors]


def rows(doc: str, con, dialect: str | None = None) -> dict:
    """Run a document through parse → payload → execute (the app's own path)."""
    ast = parse_query(doc)
    assert ast.errors == []
    return execute(con, payload_from_ast(ast), dialect)


# --- the shape the parser produces -------------------------------------------


def test_a_derived_table_carries_its_body_and_its_alias():
    ast = parse_query("\\from (\\from events \\select user_id, amount) as big\n\\limit 2")
    assert ast.errors == []
    assert ast.from_.table == ""
    assert ast.from_.alias == "big"
    assert ast.from_.body.from_.table == "events"
    assert [item.column for item in ast.from_.body.select] == ["user_id", "amount"]


def test_every_node_of_a_subquery_reports_the_clause_line():
    ast = parse_query("\\from events\n\\from (\\from users \\select user_id) as u\n\\limit 2")
    assert ast.errors == []
    assert ast.from_.body.from_.line == 2
    assert ast.from_.body.select[0].line == 2


def test_a_subquery_source_needs_an_alias():
    ast = parse_query("\\from (\\from events \\select user_id)\n\\limit 2")
    assert messages(ast) == ["a subquery source needs an alias — write ( … ) as <name>"]

    ast = parse_query("\\from events\n\\join (\\from users \\select user_id) on user_id\n\\limit 2")
    assert messages(ast) == ["a subquery source needs an alias — write ( … ) as <name>"]


def test_an_empty_body_is_named_as_such():
    ast = parse_query("\\from () as t\n\\limit 2")
    assert messages(ast) == ["a subquery body cannot be empty"]


def test_a_body_may_hold_a_subquery_of_its_own():
    ast = parse_query("\\from (\\from (\\from events \\select user_id) as i \\select i.user_id) as t\n\\limit 2")
    assert ast.errors == []
    assert ast.from_.body.from_.body.from_.table == "events"


def test_body_errors_bubble_up_and_the_nested_ast_keeps_none():
    ast = parse_query("\\from (\\from events \\where user_id = ) as t\n\\limit 2")
    assert messages(ast) == ["\\where expects `column op value`"]
    assert ast.from_.body.errors == []


def test_a_half_typed_subquery_on_the_typing_line_is_quiet():
    assert parse_query("\\from events\n\\from (\\from users").errors == []
    assert parse_query("\\from events\n\\select (\\from users \\select max(amount)").errors == []


def test_statements_are_document_only():
    assert messages(parse_query("\\from (\\from events \\temp t) as x\n\\limit 2")) == [
        "\\temp is only allowed in the document"
    ]
    assert messages(parse_query("\\from (\\from events \\begin) as x\n\\limit 2")) == [
        "\\begin is only allowed in the document"
    ]


def test_a_parenthesized_literal_stays_a_value():
    ast = parse_query("\\from events\n\\where amount = (3)\n\\limit 2")
    assert ast.errors == []
    assert ast.where.subquery is None
    assert ast.where.value == "(3)"

    # An operator that takes text keeps its operand as written, however it looks.
    ast = parse_query("\\from events\n\\where path like (\\from users \\select user_id)\n\\limit 2")
    assert ast.errors == []
    assert ast.where.subquery is None
    assert ast.where.value == "(\\from users \\select user_id)"


def test_in_without_a_subquery_says_so():
    ast = parse_query("\\from events\n\\where user_id in (3, 4)\n\\limit 2")
    assert messages(ast) == ["`in` expects an inline subquery — write ( \\from … )"]


def test_a_scalar_subquery_needs_exactly_one_column_and_an_alias():
    ast = parse_query("\\from events\n\\select (\\from users \\select user_id) as uid\n\\limit 2")
    assert ast.errors == []
    assert ast.select[0].subquery.from_.table == "users"
    assert ast.select[0].alias == "uid"

    ast = parse_query("\\from events\n\\select (\\from users \\select user_id)\n\\limit 2")
    assert messages(ast) == ["a scalar subquery needs an alias — write ( … ) as <name>"]


# --- the rows it builds ------------------------------------------------------


def test_a_derived_table_runs_the_body_as_the_source(con):
    result = rows(
        "\\from (\\from events \\select event_type, amount \\where amount > 5) as big\n"
        "\\select big.event_type\n"
        "\\select sum(amount) as total\n"
        "\\group big.event_type\n"
        "\\order total desc\n",
        con,
    )
    events = con.table("events").execute()
    want = events[events["amount"] > 5].groupby("event_type")["amount"].sum().sort_values(ascending=False)
    assert result["columns"] == ["event_type", "total"]
    assert [row[0] for row in result["rows"]] == list(want.index)
    assert [round(row[1], 9) for row in result["rows"]] == [round(value, 9) for value in want]


def test_a_derived_table_joins_and_aliases_like_a_dataset(con):
    result = rows(
        "\\from events e\n"
        "\\join (\\from users \\select user_id, region) as u on e.user_id = u.user_id\n"
        "\\select u.region\n"
        "\\select count(user_id) as n\n"
        "\\group u.region\n",
        con,
    )
    events = con.table("events").execute()
    users = con.table("users").execute()
    want = events.merge(users[["user_id", "region"]], on="user_id").groupby("region").size()
    assert dict(result["rows"]) == {region: int(n) for region, n in want.items()}


def test_a_set_operation_takes_a_subquery_operand(con):
    result = rows(
        "\\from events\n"
        "\\select event_type\n"
        "\\where event_type = \"click\"\n"
        "\\union all (\\from events \\select event_type \\where event_type = \"purchase\")\n",
        con,
    )
    events = con.table("events").execute()["event_type"].value_counts()
    assert len(result["rows"]) == int(events["click"] + events["purchase"])


def test_in_and_not_in_are_the_sets_they_name(con):
    inside = rows(
        "\\from events\n"
        "\\select user_id\n"
        "\\where user_id in (\\from users \\select user_id \\where region = \"us\")\n",
        con,
    )
    outside = rows(
        "\\from events\n"
        "\\select user_id\n"
        "\\where user_id not in (\\from users \\select user_id \\where region = \"us\")\n",
        con,
    )
    events = con.table("events").execute()
    users = con.table("users").execute()
    us = set(users.loc[users["region"] == "us", "user_id"])
    assert {row[0] for row in inside["rows"]} == us
    assert len(inside["rows"]) + len(outside["rows"]) == len(events)
    assert not ({row[0] for row in outside["rows"]} & us)


def test_a_scalar_subquery_compares_and_projects(con):
    compared = rows(
        "\\from events\n"
        "\\select amount\n"
        "\\where amount > (\\from events \\select avg(amount))\n",
        con,
    )
    projected = rows(
        "\\from events\n"
        "\\select amount, (\\from events \\select max(amount)) as peak\n",
        con,
    )
    events = con.table("events").execute()
    mean = events["amount"].mean()
    assert len(compared["rows"]) == int((events["amount"] > mean).sum())
    assert all(row[0] > mean for row in compared["rows"])
    assert projected["columns"] == ["amount", "peak"]
    assert {row[1] for row in projected["rows"]} == {round(events["amount"].max(), 9)}


def test_the_body_compiles_into_the_query_it_is(con):
    # ibis composes relations, so a body that needs no fence is inlined rather
    # than nested — what matters is that the body's clauses are all there.
    sql = expression.compile_sql(
        expression.build(
            con,
            payload_from_ast(
                parse_query(
                    "\\from (\\from events \\select event_type, amount \\where amount > 5) as big\n"
                    "\\select big.event_type\n"
                    "\\select sum(amount) as total\n"
                    "\\group big.event_type\n"
                    "\\order total desc\n"
                )
            ),
        )
    )
    assert "WHERE" in sql and "GROUP BY" in sql and "SUM(" in sql and "ORDER BY" in sql


# --- the mistakes the engine names ------------------------------------------


def test_a_wide_subquery_is_refused_by_name(con):
    payload = payload_from_ast(parse_query(
        "\\from events\n"
        "\\where user_id in (\\from users \\select user_id, region)\n"
        "\\limit 2"
    ))
    with pytest.raises(expression.PayloadError, match="needs a subquery of exactly one column"):
        expression.build(con, payload)

    payload = payload_from_ast(parse_query(
        "\\from events\n\\select (\\from users \\select user_id, region) as w\n\\limit 2"
    ))
    with pytest.raises(expression.PayloadError, match="must project exactly one column"):
        expression.build(con, payload)


def test_a_scalar_subquery_cannot_mix_with_aggregates(con):
    payload = payload_from_ast(parse_query(
        "\\from events\n"
        "\\select (\\from users \\select max(user_id)) as peak\n"
        "\\select sum(amount) as total\n"
    ))
    with pytest.raises(expression.PayloadError, match="cannot mix with aggregates or group by"):
        expression.build(con, payload)


def test_a_missing_set_op_column_names_the_operand(con):
    payload = payload_from_ast(parse_query(
        "\\from events\n\\select user_id, amount\n\\union (\\from events \\select user_id)\n"
    ))
    with pytest.raises(expression.PayloadError, match=r'\\union "\( … \)" is missing amount'):
        expression.build(con, payload)
