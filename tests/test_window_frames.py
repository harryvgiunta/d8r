"""Window frames: `rows`/`range between <bound> and <bound>` inside `over ( … )`.

The frame is the part of a window that says *which* rows the function sees, so
the tests check the rows it produces against the same arithmetic done by hand —
a rolling three-row sum, a running total, and a value-range total — not just
that a frame parses. Bounds keep one canonical spelling in the AST whichever way
they were typed.
"""

from __future__ import annotations

import pandas as pd
import pytest

from anyql.engine import expression
from anyql.engine.execute import execute
from anyql.query import parse_query, payload_from_ast


def messages(ast) -> list[str]:
    return [error.message for error in ast.errors]


def frame_of(doc: str) -> dict:
    ast = parse_query(doc)
    assert ast.errors == []
    return ast.select[0].window.frame.to_json()


# --- the shape the parser produces -------------------------------------------


def test_rows_and_range_bounds_are_read_and_canonicalized():
    assert frame_of(
        "\\from events\n"
        "\\select sum(amount) over (order by timestamp rows between unbounded preceding and current row)\n"
        "\\limit 2"
    ) == {"kind": "rows", "start": "unbounded preceding", "end": "current row"}

    # Case, spacing, and a partition all come out the same shape.
    assert frame_of(
        "\\from events\n"
        "\\select sum(amount) OVER (PARTITION BY user_id ORDER BY timestamp DESC "
        "RANGE BETWEEN   2   PRECEDING AND 1 FOLLOWING)\n"
        "\\limit 2"
    ) == {"kind": "range", "start": "2 preceding", "end": "1 following"}

    assert frame_of(
        "\\from events\n"
        "\\select sum(amount) over (order by timestamp rows between current row and unbounded following)\n"
        "\\limit 2"
    ) == {"kind": "rows", "start": "current row", "end": "unbounded following"}


def test_a_frame_requires_the_ordering_it_counts_from():
    ast = parse_query(
        "\\from events\n"
        "\\select sum(amount) over (partition by user_id rows between 1 preceding and current row)\n"
        "\\limit 2"
    )
    assert messages(ast) == [
        "over (...) frame needs order by <col> [asc|desc] and bounds of "
        "`<n> preceding`, `current row`, or `<n> following` "
        "(either end may be `unbounded preceding` / `unbounded following`)"
    ]


def test_a_bound_that_is_not_a_bound_is_refused():
    ast = parse_query(
        "\\from events\n"
        "\\select sum(amount) over (order by timestamp rows between somewhere and current row)\n"
        "\\limit 2"
    )
    assert len(ast.errors) == 1
    assert ast.errors[0].message.startswith("over (...) frame needs order by")


def test_a_frame_still_needs_a_windowed_item():
    ast = parse_query(
        "\\from events\n"
        "\\select path over (order by timestamp rows between current row and current row)\n"
        "\\limit 2"
    )
    assert messages(ast) == ["a plain column cannot carry over (...) — wrap it in a function"]


# --- the rows it builds ------------------------------------------------------


def test_rows_counts_rows_from_the_ordering(con):
    result = execute(
        con,
        payload_from_ast(
            parse_query(
                "\\from events\n"
                "\\select timestamp, amount\n"
                "\\select sum(amount) over (order by timestamp rows between 2 preceding and current row) as rolling\n"
                "\\order timestamp\n"
                "\\limit 1000\n"
            )
        ),
    )
    events = con.table("events").execute().sort_values("timestamp").reset_index(drop=True)
    want = events["amount"].rolling(3, min_periods=1).sum().tolist()
    assert result["columns"] == ["timestamp", "amount", "rolling"]
    assert [round(row[2], 9) for row in result["rows"]] == [round(value, 9) for value in want]


def test_range_counts_values_from_the_ordering(con):
    result = execute(
        con,
        payload_from_ast(
            parse_query(
                "\\from events\n"
                "\\select timestamp, amount\n"
                "\\select sum(amount) over (order by timestamp range between unbounded preceding and current row) as running\n"
                "\\order timestamp\n"
                "\\limit 1000\n"
            )
        ),
    )
    events = con.table("events").execute().sort_values("timestamp").reset_index(drop=True)
    want = events["amount"].cumsum().tolist()
    assert [round(row[2], 9) for row in result["rows"]] == [round(value, 9) for value in want]


def test_a_numeric_range_gathers_the_values_within_the_offset(con):
    result = execute(
        con,
        payload_from_ast(
            parse_query(
                "\\from events\n"
                "\\select timestamp, user_id, amount\n"
                "\\select sum(amount) over (order by user_id range between 2 preceding and current row) as nearby\n"
                "\\order timestamp\n"
                "\\limit 1000\n"
            )
        ),
    )
    events = con.table("events").execute()
    # The window is over the value of the ordering column, so equal user_ids
    # share one total — compare per user_id rather than row by row.
    got = {row[1]: round(row[3], 9) for row in result["rows"]}
    expected = {
        int(user_id): round(events.loc[events["user_id"].between(user_id - 2, user_id), "amount"].sum(), 9)
        for user_id in events["user_id"]
    }
    assert got == expected
    assert len(got) == events["user_id"].nunique()


def test_a_partitioned_frame_never_crosses_its_partition(con):
    result = execute(
        con,
        payload_from_ast(
            parse_query(
                "\\from events\n"
                "\\select timestamp, user_id, amount\n"
                "\\select sum(amount) over (partition by user_id order by timestamp "
                "rows between unbounded preceding and current row) as user_running\n"
                "\\order timestamp\n"
                "\\limit 1000\n"
            )
        ),
    )
    events = con.table("events").execute().sort_values("timestamp")
    want = events.groupby("user_id")["amount"].transform(lambda values: values.cumsum())
    assert [round(row[3], 9) for row in result["rows"]] == [round(value, 9) for value in want]


# --- the SQL it renders ------------------------------------------------------


def test_the_frame_reaches_the_compiled_sql(con):
    for kind in ("rows", "range"):
        sql = expression.compile_sql(
            expression.build(
                con,
                payload_from_ast(
                    parse_query(
                        "\\from events\n"
                        f"\\select sum(amount) over (order by timestamp {kind} between 1 preceding and 1 following) as total\n"
                        "\\limit 2"
                    )
                ),
            )
        )
        assert f"{kind.upper()} BETWEEN" in sql
        assert "1 PRECEDING" in sql.upper() and "1 FOLLOWING" in sql.upper()
