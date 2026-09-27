"""LATERAL joins and `CROSS APPLY`: the body that reads the left side.

The language's one correlated form. Its rows are checked against the same
computation written in pandas — top-N per group and a per-group total — because
"lateral" means the body runs *per left row*, and a plain join cannot say that.
The refusals matter as much as the rows: a correlation the engine cannot hoist
is named, never approximated.
"""

from __future__ import annotations

import pandas as pd
import pytest

from d8r.engine import expression
from d8r.engine.execute import execute
from d8r.query import (
    ColumnDef,
    TableDef,
    parse_query,
    payload_from_ast,
    SchemaContext,
)


@pytest.fixture
def loaded():
    """The strict regime needs a registry: `users`/`events` as the parser sees them."""
    return SchemaContext(
        [
            TableDef("events", "events", [ColumnDef("user_id", "int64"), ColumnDef("amount", "float64")]),
            TableDef("users", "users", [ColumnDef("user_id", "int64"), ColumnDef("region", "string")]),
        ]
    )


def messages(ast) -> list[str]:
    return [error.message for error in ast.errors]


def rows(doc: str, con) -> dict:
    ast = parse_query(doc)
    assert ast.errors == []
    return execute(con, payload_from_ast(ast))


def events_frame(con) -> pd.DataFrame:
    return con.table("events").execute()


# --- the shape the parser produces -------------------------------------------


def test_a_lateral_join_carries_its_body_and_its_key():
    ast = parse_query(
        "\\from users u\n"
        "\\join lateral (\\from events e \\where e.user_id = u.user_id \\order timestamp desc \\limit 2) as recent\n"
        "\\limit 2"
    )
    assert ast.errors == []
    join = ast.joins[0]
    assert join.lateral is True
    assert join.dataset == ""
    assert join.alias == "recent"
    assert (join.left, join.right) == ("", "")
    assert join.body.from_.table == "events"
    assert join.body.where.value == "u.user_id"
    assert join.body.limit == 2


def test_a_lateral_body_may_read_the_left_identifiers_only():
    # `u` is open to its left, so the body reads it; the clause's own alias is
    # not part of the left yet, exactly as for a plain `\join` key.
    ast = parse_query(
        "\\from users u\n"
        "\\join lateral (\\from events e \\where e.user_id = u.user_id) as recent\n"
        "\\limit 2"
    )
    assert ast.errors == []


def test_lateral_needs_a_body_and_the_body_needs_an_alias():
    ast = parse_query("\\from users\n\\join lateral users u on user_id = user_id\n\\limit 2")
    assert messages(ast) == [
        "\\join lateral expects `( <subquery> ) [as] <alias> [on <col>[ = <col>]]`"
    ]

    ast = parse_query("\\from users\n\\join lateral (\\from events) on user_id\n\\limit 2")
    assert messages(ast) == ["a subquery source needs an alias — write ( … ) as <name>"]


def test_a_lateral_alias_takes_its_place_among_the_open_tables():
    ast = parse_query(
        "\\from users u\n"
        "\\join lateral (\\from events) as u\n"
        "\\limit 2"
    )
    assert messages(ast) == ['duplicate table identifier "u"']

    # A body's own table may not be named like one to its left: every reference
    # to that name would be ambiguous.
    ast = parse_query(
        "\\from users u\n"
        "\\join lateral (\\from events u \\where u.user_id = u.user_id) as recent\n"
        "\\limit 2"
    )
    assert messages(ast) == [
        'duplicate table identifier "u" — a lateral body cannot reuse a name from its left'
    ]
    assert ast.joins == []


def test_outer_names_belong_to_the_immediate_body_only(loaded, con):
    # The body itself reads the left side: that is the correlation.
    outer_doc = (
        "\\from users u\n"
        "\\join lateral (\\from events e \\where e.user_id = u.user_id) as recent\n"
        "\\limit 2"
    )
    assert parse_query(outer_doc, schema=loaded).errors == []

    # A subquery nested inside the body does not: the engine correlates through
    # the immediate body's `\\where` alone, so one level deeper the same text is
    # an ordinary value again — and an int column says so, rather than the
    # document quietly correlating where it cannot.
    nested_doc = (
        "\\from users u\n"
        "\\join lateral (\\from (\\from events e \\where e.user_id = u.user_id) as i) as recent\n"
        "\\limit 2"
    )
    assert parse_query(nested_doc, schema=loaded).errors == []
    with pytest.raises(expression.PayloadError, match="cannot compare numeric column"):
        expression.build(con, payload_from_ast(parse_query(nested_doc, schema=loaded)))


# --- the rows it builds ------------------------------------------------------


def test_a_capped_lateral_body_is_per_left_row(con):
    result = rows(
        "\\from users u\n"
        "\\join lateral (\\from events e \\where e.user_id = u.user_id \\order timestamp desc \\limit 2) as recent\n"
        "\\select u.user_id, recent.timestamp, recent.amount\n",
        con,
    )
    events = events_frame(con)
    users = con.table("users").execute()
    want = events.sort_values(["user_id", "timestamp"], ascending=[True, False]).groupby("user_id").head(2)
    got = sorted((int(a), str(b), round(float(c), 9)) for a, b, c in result["rows"])
    expected = sorted(
        (int(row.user_id), row.timestamp.isoformat(), round(float(row.amount), 9))
        for row in want.itertuples()
    )
    assert got == expected
    # Every user matched, and each of them exactly twice.
    assert len(result["rows"]) == 2 * len(users)


def test_a_lateral_body_may_aggregate_per_left_row(con):
    result = rows(
        "\\from users u\n"
        "\\join lateral (\\from events e \\where e.user_id = u.user_id \\select e.user_id, sum(amount) as total) as t\n"
        "\\select u.user_id, t.total\n",
        con,
    )
    events = events_frame(con)
    want = events.groupby("user_id")["amount"].sum()
    got = sorted((int(user_id), round(float(total), 9)) for user_id, total in result["rows"])
    assert got == sorted(
        (int(user_id), round(float(total), 9)) for user_id, total in want.items()
    )


def test_cross_apply_is_the_correlated_cross_join(con):
    # No `on`: the body's own correlation is the whole predicate.
    result = rows(
        "\\from events e\n"
        "\\join lateral (\\from users u \\where u.user_id = e.user_id) as uu\n"
        "\\select e.user_id, e.amount, uu.region\n",
        con,
    )
    events = events_frame(con)
    users = con.table("users").execute()
    want = events.merge(users[["user_id", "region"]], on="user_id")
    got = sorted((int(row[0]), round(float(row[1]), 9), str(row[2])) for row in result["rows"])
    expected = sorted(
        (int(row.user_id), round(float(row.amount), 9), str(row.region)) for row in want.itertuples()
    )
    assert got == expected


def test_an_uncorrelated_body_keeps_its_own_global_limit(con):
    result = rows(
        "\\from users u\n"
        "\\join lateral (\\from events \\order timestamp desc \\limit 3) as recent\n"
        "\\select u.user_id, recent.timestamp\n",
        con,
    )
    users = con.table("users").execute()
    assert len(result["rows"]) == 3 * len(users)


def test_the_cap_compiles_to_a_row_number_filter(con):
    expr = expression.build(
        con,
        payload_from_ast(
            parse_query(
                "\\from users u\n"
                "\\join lateral (\\from events e \\where e.user_id = u.user_id \\order timestamp desc \\limit 2) as recent\n"
                "\\select u.user_id, recent.amount\n"
            )
        ),
    )
    sql = expression.compile_sql(expr)
    assert "ROW_NUMBER()" in sql and "PARTITION BY" in sql and "<= 2" in sql
    # The numbering is scaffolding, not an output column.
    assert "d8r_lateral_row" not in [str(name) for name in expr.columns]


# --- the mistakes the engine names ------------------------------------------


def test_a_correlation_that_is_not_an_equality_is_refused(con):
    payload = payload_from_ast(
        parse_query(
            "\\from users u\n"
            "\\join lateral (\\from events e \\where e.user_id != u.user_id) as recent\n"
            "\\limit 2"
        )
    )
    with pytest.raises(expression.PayloadError, match="correlates through"):
        expression.build(con, payload)


def test_a_capped_body_without_an_order_is_refused(con):
    payload = payload_from_ast(
        parse_query(
            "\\from users u\n"
            "\\join lateral (\\from events e \\where e.user_id = u.user_id \\limit 2) as recent\n"
            "\\limit 2"
        )
    )
    with pytest.raises(expression.PayloadError, match="needs \\\\order"):
        expression.build(con, payload)


def test_a_body_that_does_not_project_the_key_is_refused(con):
    payload = payload_from_ast(
        parse_query(
            "\\from users u\n"
            "\\join lateral (\\from events e \\where e.user_id = u.user_id \\select amount) as recent\n"
            "\\limit 2"
        )
    )
    with pytest.raises(expression.PayloadError, match="unknown lateral key: 'e.user_id'"):
        expression.build(con, payload)


def test_an_unknown_order_target_in_the_body_is_refused(con):
    payload = payload_from_ast(
        parse_query(
            "\\from users u\n"
            "\\join lateral (\\from events e \\where e.user_id = u.user_id \\order ghost desc \\limit 2) as recent\n"
            "\\limit 2"
        )
    )
    with pytest.raises(expression.PayloadError, match="unknown order target in the lateral body"):
        expression.build(con, payload)


def test_a_correlation_hidden_in_a_group_is_refused(con):
    payload = payload_from_ast(
        parse_query(
            "\\from users u\n"
            "\\join lateral (\\from events e \\where (e.user_id = u.user_id or e.amount > 5)) as recent\n"
            "\\limit 2"
        )
    )
    with pytest.raises(expression.PayloadError, match="cannot correlate through a"):
        expression.build(con, payload)


def test_a_second_outer_reference_is_named_not_crashed(con):
    payload = payload_from_ast(
        parse_query(
            "\\from users u\n"
            "\\join lateral (\\from events e \\where e.user_id = u.user_id"
            " and e.amount > u.user_id) as recent\n"
            "\\limit 2"
        )
    )
    with pytest.raises(expression.PayloadError, match="may not read an outer column"):
        expression.build(con, payload)


def test_a_group_in_the_residual_ride_along_the_hoist(con):
    # The hoistable equality stands ungrouped; the `( ... )` filter stays the
    # body's own — the same rows as pandas' per-user top event.
    result = rows(
        "\\from users u\n"
        "\\join lateral (\\from events e \\where (e.event_type = 'click' or e.amount > 5)"
        " and e.user_id = u.user_id \\order amount desc \\limit 1) as recent\n"
        "\\select u.user_id\n\\select recent.amount\n\\order u.user_id",
        con,
    )
    events = events_frame(con)
    users = con.table("users").execute()
    picked = (
        events[
            (events["event_type"] == "click") | (events["amount"] > 5)
        ]
        .sort_values("amount", ascending=False)
        .groupby("user_id")
        .head(1)
    )
    want = sorted(
        (int(r.user_id), float(r.amount))
        for r in picked.itertuples()
        if int(r.user_id) in set(int(u) for u in users["user_id"])
    )
    assert sorted((int(a), float(b)) for a, b in result["rows"]) == want
