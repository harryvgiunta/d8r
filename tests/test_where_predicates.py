"""The widened `\\where` predicates: `between`, `is [not] null`, `in (…)`, `and`/`or`.

Each shape is pinned twice: once as the AST the parser builds (the wire
contract), and once as the rows the engine returns — the count recomputed from
the raw Parquet with pandas, the same comparison the subquery suite uses, so
"it executes" also means "the same rows". Precedence is part of the contract:
`or` binds looser than `and`.
"""

from __future__ import annotations

import pandas as pd

from d8r.engine import expression
from d8r.engine.execute import execute
from d8r.query import parse_query, payload_from_ast

import pytest


def messages(ast) -> list[str]:
    return [error.message for error in ast.errors]


def rows(doc: str, con) -> list:
    """Run a document through parse → payload → execute (the app's own path)."""
    ast = parse_query(doc)
    assert ast.errors == []
    return execute(con, payload_from_ast(ast), None)["rows"]


def count_where(where: str, con) -> int:
    return rows(f"\\from events\n\\where {where}\n\\select count(*) as n", con)[0][0]


# --- the shape the parser produces -------------------------------------------


def test_between_carries_both_bounds():
    ast = parse_query("\\from events\n\\where amount between 5 and 10")
    assert ast.errors == []
    assert ast.where.op == "between"
    assert (ast.where.low, ast.where.high) == ("5", "10")
    assert ast.where.value == ""


def test_a_null_test_takes_no_operand():
    ast = parse_query("\\from events\n\\where user_id is null")
    assert ast.errors == []
    assert ast.where.op == "is null"
    assert ast.where.value == ""

    ast = parse_query("\\from events\n\\where user_id is not null")
    assert ast.errors == []
    assert ast.where.op == "is not null"


def test_and_builds_one_and_group():
    ast = parse_query("\\from events\n\\where event_type = \"click\" and amount > 10")
    assert ast.errors == []
    assert ast.where.column == "event_type"
    assert [c.column for c in ast.where.ands] == ["amount"]
    assert ast.where.ors == []


def test_or_binds_looser_than_and():
    ast = parse_query(
        "\\from events\n\\where event_type = \"click\" or event_type = \"view\" and amount = 0.99"
    )
    assert ast.errors == []
    # or[first, and-group] — the `and` belongs to the *second* or-branch.
    assert ast.where.ands == []
    assert len(ast.where.ors) == 1
    group = ast.where.ors[0]
    assert group.column == "event_type"
    assert [c.column for c in group.ands] == ["amount"]


def test_the_wire_shape_carries_ands_without_a_bogus_ors():
    ast = parse_query("\\from events\n\\where user_id > 1 and user_id < 9")
    where = ast.to_json()["where"]
    assert "ands" in where
    assert "ors" not in where  # an empty or-list is a payload error, not an absence

    ast = parse_query("\\from events\n\\where user_id > 1 or user_id < 9")
    where = ast.to_json()["where"]
    assert "ands" not in where
    assert [group[0]["column"] for group in where["ors"]] == ["user_id"]


def test_malformed_new_predicates_say_so():
    # Settled documents: the malformed clause must not sit on the typing line.
    assert messages(parse_query("\\from events\n\\where amount between 5\n\\limit 2")) == [
        "`between` needs low and high"
    ]
    assert messages(parse_query("\\from events\n\\where user_id is null 3\n\\limit 2")) == [
        "`is null` takes no operand"
    ]
    # A lone trailing `and` leaves an empty piece, which is not a condition.
    assert messages(parse_query("\\from events\n\\where user_id > 1 and\n\\limit 2")) == [
        "\\where expects `column op value`"
    ]


def test_a_half_typed_condition_on_the_typing_line_is_quiet():
    assert parse_query("\\from events\n\\where amount between 5 and").errors == []
    assert parse_query("\\from events\n\\where amount > 1 or ").errors == []
    assert parse_query("\\from events\n\\where user_id is ").errors == []


def test_joiners_hide_inside_quotes_and_parens():
    # `and` inside a quoted value, and inside a subquery, never join conditions.
    ast = parse_query("\\from events\n\\where path = \"rock and roll\" or user_id = 1")
    assert ast.errors == []
    assert ast.where.value == "rock and roll"
    assert len(ast.where.ors) == 1

    ast = parse_query(
        "\\from events\n\\where user_id in (\\from users \\select user_id \\where region = \"us and eu\") and amount > 1"
    )
    assert ast.errors == []
    assert [c.column for c in ast.where.ands] == ["amount"]


def test_betweens_own_and_never_reads_as_a_joiner():
    ast = parse_query("\\from events\n\\where amount between 5 and 10 and user_id = 3")
    assert ast.errors == []
    assert ast.where.op == "between"
    assert [c.column for c in ast.where.ands] == ["user_id"]



def test_a_parenthesized_piece_is_a_group_node():
    ast = parse_query("\\from events\n\\where (user_id = 1 or user_id = 2) and amount > 1")
    assert ast.errors == []
    head = ast.where
    assert head.group is not None
    assert (head.column, head.op, head.value) == ("", "", "")
    assert [c.column for c in head.ands] == ["amount"]
    # The group's own tree: head `user_id = 1`, one or-group `user_id = 2`.
    assert head.group.column == "user_id"
    assert [g.column for g in head.group.ors] == ["user_id"]
    # conditions() sees every leaf wherever nesting put it.
    assert [c.op for c in ast.where.conditions()] == ["=", "=", ">"]


def test_groups_change_what_the_tree_means():
    grouped = parse_query(
        "\\from events\n\\where (event_type = 'click' or event_type = 'view') and amount > 20"
    )
    assert grouped.errors == []
    assert grouped.where.group is not None  # the or lives inside the parens
    assert grouped.where.ors == []

    flat = parse_query(
        "\\from events\n\\where event_type = 'click' or event_type = 'view' and amount > 20"
    )
    assert flat.errors == []
    assert flat.where.group is None
    assert len(flat.where.ors) == 1  # the unparenthesized `or` splits the line


def test_a_group_nested_in_a_group_nests_its_tree():
    ast = parse_query(
        "\\from events\n\\where (user_id = 1 or (amount > 5 and amount < 10))"
    )
    assert ast.errors == []
    outer = ast.where.group  # the whole line is one group
    assert outer.column == "user_id"
    inner = outer.ors[0]
    assert inner.group is not None
    assert inner.group.column == "amount"
    assert [c.op for c in ast.where.conditions()] == ["=", ">", "<"]


def test_a_group_survives_the_wire_shapes():
    ast = parse_query("\\from events\n\\where (user_id = 1 or amount > 5) and path is null")
    cond = ast.to_json()["where"]
    assert cond["column"] == "" and "group" in cond
    assert cond["ands"][0]["op"] == "is null"


def test_malformed_groups_say_so():
    assert messages(parse_query("\\from events\n\\where (user_id = 1 and\n\\limit 2")) == [
        "`(...)` group is unfinished — close it with )"
    ]
    assert messages(parse_query("\\from events\n\\where () and user_id = 1\n\\limit 2")) == [
        "`(...)` group cannot be empty"
    ]
    assert messages(parse_query("\\from events\n\\where (user_id = 1) junk\n\\limit 2")) == [
        "`(...)` group has unexpected `junk` after its )"
    ]


def test_an_unfinished_group_on_the_typing_line_is_quiet():
    assert parse_query("\\from events\n\\where (user_id = 1 or ").errors == []
    assert parse_query("\\from events\n\\where (").errors == []


# --- the rows the engine returns ----------------------------------------------


def test_groups_match_the_pandas_mask(con):
    events = pd.read_parquet("d8r/engine/data/events.parquet")
    want = int(
        (((events.event_type == "click") | (events.event_type == "view")) & (events.amount > 20)).sum()
    )
    # Parenthesized: the `or` is hoisted above the `and`. Unparenthesized, the
    # same words mean `(click) or (view and >20)` — a different row set.
    assert count_where('event_type = "click" or event_type = "view" and amount > 20', con) != want
    assert count_where('(event_type = "click" or event_type = "view") and amount > 20', con) == want


def test_a_group_as_an_or_segment_matches_pandas(con):
    events = pd.read_parquet("d8r/engine/data/events.parquet")
    want = int((
        (events.event_type.isin(["click", "view"])) | (events.user_id == 1)
    ).sum())
    assert count_where(
        '(event_type in ("click", "view")) or user_id = 1', con
    ) == want


def test_a_group_inside_a_group_matches_pandas(con):
    events = pd.read_parquet("d8r/engine/data/events.parquet")
    want = int(
        ((events.event_type == "click") | ((events.user_id == 4) & (events.amount < 2))).sum()
    )
    assert count_where(
        'event_type = "click" or (user_id = 4 and amount < 2)', con
    ) == want




def test_between_matches_the_pandas_mask(con):
    events = pd.read_parquet("d8r/engine/data/events.parquet")
    want = int(((events.amount >= 5) & (events.amount <= 10)).sum())
    assert count_where("amount between 5 and 10", con) == want


def test_in_list_matches_the_pandas_mask(con):
    events = pd.read_parquet("d8r/engine/data/events.parquet")
    want = int(events.user_id.isin([3, 4, 5]).sum())
    assert count_where("user_id in (3, 4, 5)", con) == want
    want_text = int(events.event_type.isin(["click", "view"]).sum())
    assert count_where('event_type in ("click", "view")', con) == want_text


def test_or_and_and_precedence_match_pandas(con):
    events = pd.read_parquet("d8r/engine/data/events.parquet")
    want = int(
        ((events.event_type == "click") | ((events.event_type == "view") & (events.amount == 0.99))).sum()
    )
    assert count_where('event_type = "click" or event_type = "view" and amount = 0.99', con) == want

    want_mixed = int(
        (((events.event_type == "purchase")) & events.amount.between(5, 10) | (events.user_id.isin([3, 4, 5]))).sum()
    )
    assert count_where(
        'event_type = "purchase" and amount between 5 and 10 or user_id in (3, 4, 5)', con
    ) == want_mixed


def test_null_tests_read_the_column(con):
    assert count_where("user_id is not null", con) == 100  # the demo column has no NULLs
    assert count_where("user_id is null", con) == 0
    assert count_where("user_id is null or user_id = 1", con) == 4  # four user_id == 1 rows


def test_a_list_value_of_the_wrong_type_is_the_engines_error(con):
    payload = payload_from_ast(parse_query("\\from events\n\\where user_id in (\"x\")"))
    with pytest.raises(expression.PayloadError, match="cannot compare numeric column"):
        expression.build(con, payload)
