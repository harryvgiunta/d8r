"""Expression compilation + execute end-to-end against the real datasets."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from d8r.engine import add_sqlite_source, expression
from d8r.engine.execute import execute
from d8r.query import parse_query, payload_from_ast

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

PURCHASES_SQL_PAYLOAD = {
    "dataset": "events",
    "select": [
        {"column": "event_type", "aggregate": None, "alias": None},
        {"column": None, "aggregate": {"fn": "sum", "arg": "amount"}, "alias": "revenue"},
    ],
    "where": {"column": "event_type", "op": "=", "value": "purchase"},
    "groupBy": ["event_type"],
    "orderBy": [{"target": "revenue", "direction": "desc"}],
    "limit": 10,
}


def load(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def test_purchases_payload_compiles_with_all_clauses(con):
    expr = expression.build(con, PURCHASES_SQL_PAYLOAD)
    sql = expression.compile_sql(expr).upper()
    for clause in ("WHERE", "GROUP BY", "ORDER BY", "LIMIT 10"):
        assert clause in sql
    assert list(expr.schema().names) == ["event_type", "revenue"]


def test_purchases_payload_executes(con):
    result = expression.build(con, PURCHASES_SQL_PAYLOAD).execute()
    assert result.shape[0] == 1
    assert float(result["revenue"][0]) > 0
    assert result["event_type"][0] == "purchase"


def test_like_is_the_sql_pattern_match_and_ilike_ignores_case(con):
    """`like` hands the pattern to SQL untouched: `%`/`_` are wildcards, no `%`
    is stripped, and a bare substring matches nothing it was not written to."""
    def where(value: str, op: str = "like") -> int:
        payload = {
            "dataset": "events",
            "select": [],
            "where": {"column": "path", "op": op, "value": value},
            "groupBy": [],
            "orderBy": [],
            "limit": None,
        }
        return expression.build(con, payload).execute().shape[0]

    assert where("%/P/%") == 0            # case-sensitive: no path has /P/
    assert where("%/p/1%") == 11          # i % 9 == 1: i=1..91
    assert where("/p/1%") == 11           # anchored prefix
    assert where("p/1") == 0              # containment would have matched 11
    assert where("_p_1") == 11           # `_` matches the leading slash and the middle slash
    assert where("/p/1_") == 0           # one character too many
    assert where("%/P/%", "ilike") == 100  # case-insensitive covers every row
    assert where("p/1", "ilike") == 0

    payload = {
        "dataset": "events",
        "select": [],
        "where": {"column": "path", "op": "like", "value": "/p/1%"},
        "groupBy": [],
        "orderBy": [],
        "limit": None,
    }
    sql = expression.compile_sql(expression.build(con, payload))
    assert "LIKE '/p/1%'" in sql          # the pattern verbatim, not `%…%`
    payload["where"]["op"] = "ilike"
    sql = expression.compile_sql(expression.build(con, payload))
    assert "ILIKE" in sql.upper() and "LIKE" in sql.upper()


def test_first10_execute_matches_fixture(con):
    from tests.conftest import FIRST10_PAYLOAD

    result = execute(con, FIRST10_PAYLOAD)
    expected = load("expected_rows.json")
    assert result["columns"] == expected["columns"]
    assert result["rows"] == expected["rows"]

@pytest.fixture
def count_source(tmp_path):
    path = tmp_path / "counts.sqlite"
    with sqlite3.connect(path) as raw:
        raw.execute("create table usage_events (id integer, project_id integer, total_tokens integer)")
        raw.executemany(
            "insert into usage_events values (?, ?, ?)",
            [(11, 1, 10), (12, 1, None), (13, 2, None), (14, 3, 50), (15, 4, 100), (16, 1, 5)],
        )
        raw.execute("create table projects (id integer, name text, primary_email text, plan_id text)")
        raw.executemany(
            "insert into projects values (?, ?, ?, ?)",
            [
                (1, "First", None, "free"),
                (1, "First", None, "free"),  # Each matching event contributes twice.
                (2, "Second", "second@example.test", "free"),
                (3, "Paid", "paid@example.test", "paid"),
            ],
        )
    raw.close()
    source = add_sqlite_source("counts", str(path))
    try:
        yield source.con
    finally:
        source.con.disconnect()


COUNT_JOIN = (
    "\\from usage_events as ue\n"
    "\\join projects as p on ue.project_id = p.id\n"
    "\\where p.plan_id = 'free'\n"
)


def test_count_star_groups_current_joined_filtered_relation(count_source):
    ast = parse_query(
        COUNT_JOIN
        + "\\select p.id, p.name, p.primary_email, count(*) as total_requests, "
        "sum(ue.total_tokens) as total_tokens\n"
        "\\group p.id, p.name, p.primary_email\n"
        "\\order total_requests desc\n\\limit 20"
    )
    assert ast.errors == []
    result = execute(count_source, payload_from_ast(ast), dialect="sqlite")
    assert result["columns"] == ["id", "name", "primary_email", "total_requests", "total_tokens"]
    assert result["rows"] == [
        [1, "First", None, 6, 30],
        [2, "Second", "second@example.test", 1, None],
    ]


@pytest.mark.parametrize("plan, expected", [("free", [7, 4]), ("missing", [0, 0])])
def test_count_star_global_keeps_null_rows_and_empty_input(count_source, plan, expected):
    ast = parse_query(
        COUNT_JOIN.replace("'free'", f"'{plan}'")
        + "\\select count(*), count(ue.total_tokens) as nonnull\n\\order __count"
    )
    assert ast.errors == []
    result = execute(count_source, payload_from_ast(ast), dialect="sqlite")
    assert result["columns"] == ["__count", "nonnull"]
    assert result["rows"] == [expected]


def test_count_star_windows_count_joined_filtered_rows_and_respect_frames(count_source):
    ast = parse_query(
        COUNT_JOIN
        + "\\select ue.id, p.id as project, count(*) over (partition by p.id), "
        "count(ue.total_tokens) over (partition by p.id) as nonnull, "
        "count(*) over (partition by p.id order by ue.id "
        "range between unbounded preceding and current row) as running\n"
        "\\order id"
    )
    assert ast.errors == []
    result = execute(count_source, payload_from_ast(ast), dialect="sqlite")
    assert result["columns"] == ["id", "project", "__count", "nonnull", "running"]
    assert result["rows"] == [
        [11, 1, 6, 4, 2], [11, 1, 6, 4, 2],
        [12, 1, 6, 4, 4], [12, 1, 6, 4, 4],
        [13, 2, 1, 0, 1],
        [16, 1, 6, 4, 6], [16, 1, 6, 4, 6],
    ]


@pytest.mark.parametrize("call", ["sum(*)", "avg(*) over (partition by user_id)"])
def test_non_count_aggregate_wildcard_is_an_aggregate_error(con, call):
    ast = parse_query(f"\\from events\n\\select {call}\n\\limit 1")
    assert ast.errors == []
    with pytest.raises(expression.PayloadError, match="only count accepts"):
        expression.build(con, payload_from_ast(ast))



def test_payload_errors(con):
    with pytest.raises(expression.PayloadError, match="unknown dataset"):
        expression.build(con, {"dataset": "nope"})
    with pytest.raises(expression.PayloadError, match="unknown column"):
        expression.build(
            con, {"dataset": "events", "select": [{"column": "bogus"}]}
        )
    with pytest.raises(expression.PayloadError, match="unknown operator"):
        expression.build(
            con,
            {
                "dataset": "events",
                "where": {"column": "user_id", "op": "<>", "value": 1},
            },
        )
    with pytest.raises(expression.PayloadError, match="needs a string column"):
        expression.build(
            con,
            {
                "dataset": "events",
                "where": {"column": "user_id", "op": "~", "value": "1"},
            },
        )
    with pytest.raises(expression.PayloadError, match="unknown aggregate"):
        expression.build(
            con,
            {"dataset": "events", "select": [{"aggregate": {"fn": "median", "arg": "amount"}}]},
        )
    with pytest.raises(expression.PayloadError, match="non-negative"):
        expression.build(con, {"dataset": "events", "limit": -1})
    with pytest.raises(expression.PayloadError, match="orderBy target"):
        expression.build(
            con, {"dataset": "events", "orderBy": [{"target": "ghost", "direction": "asc"}]}
        )


def test_empty_select_returns_all_columns(con):
    expr = expression.build(con, {"dataset": "users", "limit": 3})
    assert list(expr.schema().names) == ["user_id", "region", "joined", "score"]
    assert expr.execute().shape[0] == 3


def test_numeric_coercion_of_string_literal(con):
    expr = expression.build(
        con,
        {
            "dataset": "events",
            "where": {"column": "user_id", "op": "=", "value": "7"},
            "limit": 5,
        },
    )
    frame = expr.execute()
    assert not frame.empty
    assert set(frame["user_id"]) == {7}


def test_join_payload_compiles_with_join_sql(con):
    from tests.conftest import JOIN_REVENUE_PAYLOAD

    expr = expression.build(con, JOIN_REVENUE_PAYLOAD)
    sql = expression.compile_sql(expr).upper()
    assert "JOIN" in sql
    assert list(expr.schema().names) == ["region", "revenue"]


def test_join_qualified_refs_filter_by_right_table_column(con):
    expr = expression.build(
        con,
        {
            "dataset": "events",
            "joins": [{"dataset": "users", "left": "user_id", "right": "user_id"}],
            "select": [{"column": "users.region"}, {"column": "events.amount"}],
            "where": {"column": "users.score", "op": ">", "value": "50"},
            "limit": 3,
        },
    )
    assert list(expr.schema().names) == ["region", "amount"]
    frame = expr.execute()
    assert not frame.empty
    # ground-truth the filter: every emitted (region, amount) pairs with score > 50
    events = con.table("events")
    users = con.table("users")
    joined = (
        events.join(users, events.user_id == users.user_id)
        .select(users.region, events.amount, users.score)
        .execute()
    )
    good = {
        (row.region, row.amount) for row in joined.itertuples() if row.score > 50
    }
    for region, amount in frame.itertuples(index=False, name=None):
        assert (region, amount) in good


def test_join_star_projection_dedups_shared_key(con):
    expr = expression.build(
        con,
        {
            "dataset": "events",
            "joins": [{"dataset": "users", "left": "user_id", "right": "user_id"}],
            "limit": 3,
        },
    )
    assert list(expr.schema().names) == [
        "timestamp",
        "user_id",
        "event_type",
        "amount",
        "path",
        "region",
        "joined",
        "score",
    ]  # user_id once, from events (left)


def test_join_inner_row_conservation(con):
    frame = (
        expression.build(
            con,
            {
                "dataset": "events",
                "joins": [{"dataset": "users", "left": "user_id", "right": "user_id"}],
                "limit": 200,
            },
        )
        .execute()
    )
    assert frame.shape[0] == 100  # every events.user_id exists in users


def test_join_payload_errors(con):
    with pytest.raises(expression.PayloadError, match="unknown dataset"):
        expression.build(
            con,
            {
                "dataset": "events",
                "joins": [{"dataset": "ghost", "left": "user_id", "right": "user_id"}],
            },
        )
    with pytest.raises(expression.PayloadError, match="join key"):
        expression.build(
            con,
            {
                "dataset": "events",
                "joins": [{"dataset": "users", "left": "ghost", "right": "user_id"}],
            },
        )
    with pytest.raises(expression.PayloadError, match="join key"):
        expression.build(
            con,
            {
                "dataset": "events",
                "joins": [{"dataset": "users", "left": "user_id", "right": "ghost"}],
            },
        )
    with pytest.raises(expression.PayloadError, match="unknown column"):
        expression.build(
            con,
            {
                "dataset": "events",
                "joins": [{"dataset": "users", "left": "user_id", "right": "user_id"}],
                "select": [{"column": "scores.pointless"}],
            },
        )
    with pytest.raises(expression.PayloadError, match="unknown column"):
        expression.build(
            con,
            {
                "dataset": "events",
                "joins": [{"dataset": "users", "left": "user_id", "right": "user_id"}],
                "select": [{"column": "events.region"}],  # region lives on users only
            },
        )


def test_alias_join_compiles_and_matches_fixture(con):
    from tests.conftest import ALIAS_REVENUE_PAYLOAD

    expr = expression.build(con, ALIAS_REVENUE_PAYLOAD)
    assert "JOIN" in expression.compile_sql(expr).upper()
    assert list(expr.schema().names) == ["region", "amount_sum"]
    result = execute(con, ALIAS_REVENUE_PAYLOAD)
    fixture = load("expected_alias_rows.json")
    assert result["columns"] == fixture["columns"]
    assert result["rows"] == fixture["rows"]


def test_aggregate_auto_alias_no_joins(con):
    expr = expression.build(
        con,
        {
            "dataset": "events",
            "select": [
                {"column": "event_type", "aggregate": None, "alias": None},
                {"column": None, "aggregate": {"fn": "sum", "arg": "amount"}, "alias": None},
            ],
            "groupBy": ["event_type"],
            "orderBy": [{"target": "amount_sum", "direction": "desc"}],
        },
    )
    assert list(expr.schema().names) == ["event_type", "amount_sum"]
    frame = expr.execute()
    assert list(frame["amount_sum"]) == sorted(frame["amount_sum"], reverse=True)


def test_explicit_alias_beats_auto_alias(con):
    expr = expression.build(
        con,
        {
            "dataset": "events",
            "select": [{"column": None, "aggregate": {"fn": "sum", "arg": "amount"}, "alias": "x"}],
        },
    )
    assert list(expr.schema().names) == ["x"]


def test_derived_alias_rule():
    assert expression._derived_alias("sum", "users.amount") == "amount_sum"
    assert expression._derived_alias("AVG", "Amount") == "amount_avg"
    assert expression._derived_alias("count", "user-id") == "user_id_count"
    assert expression._derived_alias("min", None) == "arg_min"


def test_temporal_part_is_a_grouping_column(con):
    # `year(timestamp)` alongside an aggregate: the extracted part is a derived
    # grouping key (auto-grouped with the plain columns), not an aggregate.
    expr = expression.build(
        con,
        {
            "dataset": "events",
            "select": [
                {"column": None, "temporal": {"fn": "year", "arg": "timestamp"}},
                {"column": None, "aggregate": {"fn": "sum", "arg": "amount"}},
            ],
        },
    )
    assert list(expr.schema().names) == ["timestamp_year", "amount_sum"]
    sql = expression.compile_sql(expr)
    assert "EXTRACT(year" in sql and "GROUP BY" in sql
    frame = expr.execute()
    # The part column is integer, one row per year, sum re-aggregated per group.
    assert frame["timestamp_year"].dtype.kind == "i"
    assert frame["timestamp_year"].nunique() == len(frame)


def test_temporal_part_alone_is_not_grouped(con):
    expr = expression.build(
        con,
        {
            "dataset": "events",
            "select": [{"column": None, "temporal": {"fn": "month", "arg": "timestamp"}}],
            "limit": 4,
        },
    )
    assert list(expr.schema().names) == ["timestamp_month"]
    assert "GROUP BY" not in expression.compile_sql(expr)
    assert expr.execute().shape[0] == 4  # row-wise extraction, not collapsed


def test_temporal_explicit_alias_wins(con):
    expr = expression.build(
        con,
        {
            "dataset": "users",
            "select": [{"column": None, "temporal": {"fn": "year", "arg": "joined"}, "alias": "y"}],
            "limit": 1,
        },
    )
    assert list(expr.schema().names) == ["y"]


def test_temporal_families_are_time_scoped():
    # The family table is the completion contract: time-of-day parts belong to
    # timestamp/time only, calendar parts are shared. (No DATE/TIME column
    # exists in the bundled datasets; the dtype check in `build` keys off this
    # same table, so a date column would reject `hour` exactly like here.)
    assert expression.TEMPORAL_FNS["timestamp"] >= expression.TEMPORAL_FNS["date"]
    assert expression.TEMPORAL_FNS["timestamp"] >= expression.TEMPORAL_FNS["time"]
    assert not ({"hour", "minute", "second"} & expression.TEMPORAL_FNS["date"])
    assert not ({"year", "month", "day"} & expression.TEMPORAL_FNS["time"])


def test_temporal_on_non_temporal_column_rejected(con):
    with pytest.raises(expression.PayloadError, match="unknown function"):
        expression.build(
            con,
            {
                "dataset": "events",
                "select": [{"column": None, "temporal": {"fn": "year", "arg": "amount"}}],
            },
        )


def test_temporal_unknown_fn_rejected(con):
    with pytest.raises(expression.PayloadError, match="unknown function"):
        expression.build(
            con,
            {
                "dataset": "events",
                "select": [{"column": None, "temporal": {"fn": "week", "arg": "timestamp"}}],
            },
        )


def test_alias_prefix_is_strict(con):
    expr = expression.build(
        con,
        {"dataset": "events", "alias": "e", "select": [{"column": "e.user_id"}], "limit": 2},
    )
    assert list(expr.schema().names) == ["user_id"]
    with pytest.raises(expression.PayloadError, match="unknown column"):
        expression.build(
            con,
            {"dataset": "events", "alias": "e", "select": [{"column": "events.user_id"}]},
        )


def test_duplicate_table_identifier_rejected(con):
    with pytest.raises(expression.PayloadError, match='duplicate table identifier "users"'):
        expression.build(
            con,
            {
                "dataset": "events",
                "alias": "users",
                "joins": [{"dataset": "users", "left": "user_id", "right": "user_id"}],
            },
        )
    with pytest.raises(expression.PayloadError, match='duplicate table identifier "u"'):
        expression.build(
            con,
            {
                "dataset": "events",
                "alias": "e",
                "joins": [
                    {"dataset": "users", "alias": "u", "left": "user_id", "right": "user_id"},
                    {"dataset": "users", "alias": "u", "left": "user_id", "right": "user_id"},
                ],
            },
        )



# --- star select --------------------------------------------------------------


def test_star_select_projects_all_columns(con):
    expr = expression.build(con, {"dataset": "events", "select": [{"star": True}], "limit": 2})
    assert list(expr.schema().names) == ["timestamp", "user_id", "event_type", "amount", "path"]
    assert expr.execute().shape[0] == 2


def test_star_after_plain_column_dedupes(con):
    # `\select user_id, *` keeps user_id at its position, then the remaining
    # columns in table order — the star expansion skips already-projected names.
    payload = {
        "dataset": "events",
        "select": [{"column": "user_id"}, {"star": True}],
        "limit": 1,
    }
    expr = expression.build(con, payload)
    assert list(expr.schema().names) == ["user_id", "timestamp", "event_type", "amount", "path"]


def test_star_mixes_with_case(con):
    payload = {
        "dataset": "users",
        "select": [{"star": True}],
        "cases": [{"alias": "big", "whens": [{"column": "score", "op": ">", "value": 5, "then": 1}], "else": 0}],
        "limit": 1,
    }
    expr = expression.build(con, payload)
    assert list(expr.schema().names) == ["user_id", "region", "joined", "score", "big"]


def test_star_errors(con):
    for extra, message in [
        ({"window": {"partitionBy": ["user_id"], "order": None}}, "cannot carry over"),
        ({"alias": "x"}, "cannot take an alias"),
        ({"column": "user_id"}, "carries no expression"),
    ]:
        item = {"star": True, **extra}
        with pytest.raises(expression.PayloadError, match=message):
            expression.build(con, {"dataset": "events", "select": [item], "limit": 1})
    # star + aggregate is the same mix rule the windowed items follow
    with pytest.raises(expression.PayloadError, match="star select cannot mix"):
        expression.build(
            con,
            {
                "dataset": "events",
                "select": [{"star": True}, {"aggregate": {"fn": "sum", "arg": "amount"}}],
            },
        )


def test_constant_projection_preserves_quoted_text_and_row_cardinality(con):
    doc = """\\from events
\\select '1', 'x', 1 as number, -2.5 as decimal, .5e2 as exponent, true as yes, false as no, null as missing, '' as empty, 'it''s, as over (x)' as quoted, "double" as double_text, user_id
\\limit 2"""
    ast = parse_query(doc)
    assert ast.errors == []
    result = execute(con, payload_from_ast(ast))
    expected = ["1", "x", 1, -2.5, 50.0, True, False, None, "", "it's, as over (x)", "double"]
    assert result["columns"] == ["'1'", "'x'", "number", "decimal", "exponent", "yes", "no", "missing", "empty", "quoted", "double_text", "user_id"]
    assert [row[:-1] for row in result["rows"]] == [expected, expected]
    assert result["rows"][0][0] == "1" and type(result["rows"][0][2]) is int


def test_constants_do_not_turn_an_empty_global_aggregate_into_grouping(con):
    doc = "\\from events\n\\select count(user_id) as n, 'x' as marker\n\\where user_id < 0"
    result = execute(con, payload_from_ast(parse_query(doc)))
    assert result["columns"] == ["n", "marker"]
    assert result["rows"] == [[0, "x"]]

    # Without aggregation, a constant still projects one value per source row.
    doc = "\\from events\n\\select 'x' as marker\n\\where user_id < 0"
    assert execute(con, payload_from_ast(parse_query(doc)))["rows"] == []


def test_constants_compose_with_grouped_aggregates(con):
    doc = "\\from events\n\\select event_type, count(user_id) as n, '1' as marker\n\\group event_type\n\\order event_type"
    result = execute(con, payload_from_ast(parse_query(doc)))
    baseline = execute(con, payload_from_ast(parse_query(doc.replace(", '1' as marker", ""))))
    assert result["rows"] == [[*row, "1"] for row in baseline["rows"]]
