"""Row-level distinct after projection/set operations, before ordering and caps."""

from __future__ import annotations

import sqlite3
from collections import Counter

import pytest

from d8r.engine import add_sqlite_source, expression
from d8r.engine.execute import execute
from d8r.query import EMPTY_SCHEMA, FnDef, SchemaContext, clause_line, parse_query, payload_from_ast


def run(doc: str, con, *, schema=EMPTY_SCHEMA, **kwargs) -> dict:
    ast = parse_query(doc, schema=schema)
    assert ast.errors == []
    return execute(con, payload_from_ast(ast), **kwargs)


def test_distinct_and_unique_deduplicate_projected_rows(con):
    doc = "\\from events\n\\select event_type\n\\distinct\n\\order event_type"
    expected = [["click"], ["purchase"], ["signup"], ["view"]]
    assert run(doc, con)["rows"] == expected
    assert run(doc.replace("\\distinct", "\\UNIQUE"), con)["rows"] == expected
    # The original rows have different timestamps, so source-row dedup is not enough.
    assert run("\\from events\n\\distinct\n\\select count(user_id) as n", con)["rows"] == [[100]]


def test_aliases_repeat_idempotently_and_share_block_scoped_clause_lookup():
    doc = "\\with kinds\n  \\from events\n  \\unique\n\\from kinds\n\\distinct\n\\UNIQUE"
    ast = parse_query(doc)
    assert ast.errors == []
    assert ast.distinct is True and ast.with_[0].body.distinct is True
    assert ast.to_json() == parse_query(doc.replace("unique", "distinct").replace("UNIQUE", "distinct")).to_json()
    for name in ("distinct", "unique"):
        assert clause_line(doc, 3, name) == 3
        assert clause_line(doc, 6, name) == 6
        assert clause_line("\\with kinds\n  \\unique\n\\from kinds", 3, name) is None


def test_computed_outputs_participate_in_row_equality(con):
    result = run(
        "\\from events\n\\select concat(upper(event_type), '!') as kind\n"
        "\\case band = when user_id > 10 then 'high' else 'low'\n"
        "\\distinct\n\\order kind\n\\order band",
        con,
    )
    assert result["columns"] == ["kind", "band"]
    assert result["rows"] == [
        [kind, band]
        for kind in ("CLICK!", "PURCHASE!", "SIGNUP!", "VIEW!")
        for band in ("high", "low")
    ]


def test_distinct_runs_after_union_all_and_before_order_and_limit(con):
    doc = (
        "\\from events\n\\select event_type\n\\distinct\n"
        "\\union all (\\from events \\select event_type)\n"
        "\\order event_type desc\n\\limit 3"
    )
    assert run(doc, con)["rows"] == [["view"], ["signup"], ["purchase"]]
    # Removing the row-level command must not change the set operation's ALL flag.
    assert run(doc.replace("\\distinct\n", ""), con)["rows"] == [["view"]] * 3


def test_cte_and_inline_distinct_are_local_to_their_query_block(con):
    cte = (
        "\\with kinds\n  \\from events\n  \\select event_type\n  \\distinct\n"
        "\\from kinds\n\\union all kinds\n\\order event_type"
    )
    assert run(cte, con)["rows"] == [
        [kind] for kind in ("click", "purchase", "signup", "view") for _ in range(2)
    ]
    assert run(
        "\\from (\\from events \\select event_type \\unique) as kinds\n"
        "\\select count(event_type) as n",
        con,
    )["rows"] == [[4]]
    # Outer distinct must not deduplicate the inner input before its limit.
    assert run(
        "\\from (\\from events \\select event_type \\order event_type \\limit 2) as kinds\n"
        "\\distinct",
        con,
    )["rows"] == [["click"]]


def test_function_body_distinct_survives_argument_expansion(con):
    schema = SchemaContext(fns=[FnDef(
        "kinds",
        params=["minimum"],
        body="\\from events\n\\where user_id > @minimum\n\\select event_type\n\\unique",
    )])
    assert run("\\from kinds(0)\n\\select count(event_type) as n", con, schema=schema)["rows"] == [[4]]


def test_sqlite_tuple_null_and_empty_input_semantics(tmp_path):
    path = tmp_path / "distinct.sqlite"
    with sqlite3.connect(path) as raw:
        raw.execute("create table pairs (k integer, word text)")
        raw.executemany(
            "insert into pairs values (?, ?)",
            [(1, "a"), (1, "a"), (1, "b"), (2, "a"), (None, "a"), (None, "a")],
        )
    source = add_sqlite_source("distinct", str(path))
    tables = {name: dataset["table"] for name, dataset in source.datasets.items()}
    try:
        # Full-row equality keeps (1, a), (1, b), and (2, a) independently.
        whole = "\\from pairs\n\\distinct"
        expected = Counter([(None, "a"), (1, "a"), (1, "b"), (2, "a")])
        assert Counter(map(tuple, run(whole, source.con, tables=tables)["rows"])) == expected
        assert Counter(map(tuple, run(whole + "\n\\select *", source.con, tables=tables)["rows"])) == expected
        projected = "\\from pairs\n\\select word\n\\unique\n\\order word"
        assert run(projected, source.con, tables=tables)["rows"] == [["a"], ["b"]]
        assert run(projected + "\n\\where k < 0", source.con, tables=tables)["rows"] == []
        assert run(
            "\\from pairs\n\\select null as missing, '' as empty\n\\distinct",
            source.con,
            tables=tables,
        )["rows"] == [[None, ""]]
    finally:
        source.con.disconnect()


@pytest.mark.parametrize("clause", [
    "\\distinct event_type",
    "\\unique(event_type)",
    "\\select distinct event_type",
    "\\select distinct(event_type)",
    "\\select count(distinct event_type)",
])
def test_distinct_does_not_expand_select_or_aggregate_syntax(clause):
    doc = f"\\from events\n{clause}"
    assert parse_query(doc).errors == []  # still being typed
    ast = parse_query(doc + "\n\\limit 2")
    assert [error.line for error in ast.errors] == [2]
    assert ast.distinct is False


@pytest.mark.parametrize("value", [None, 1, "true"])
def test_direct_payload_distinct_requires_a_boolean(con, value):
    with pytest.raises(expression.PayloadError):
        expression.build(con, {"dataset": "events", "distinct": value})
