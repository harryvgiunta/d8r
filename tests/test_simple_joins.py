"""Alias-graph joins preserve written orientation and real relational semantics."""

from __future__ import annotations

from collections import Counter

import ibis
import pytest

from d8r.engine.execute import execute
from d8r.query import ColumnDef, SchemaContext, TableDef, parse_query, payload_from_ast


@pytest.fixture
def join_db():
    con = ibis.duckdb.connect()
    data = {
        "left_rows": {
            "id": [1, 2, 2, 3],
            "bucket": ["x", "x", "y", "x"],
            "label": ["a1", "a2x", "a2y", "a3"],
        },
        "right_rows": {
            "id": [2, 2, 4],
            "bucket": ["x", "y", "x"],
            "label": ["b2x", "b2y", "b4"],
        },
        "third_rows": {"id": [1, 2, 4, 5], "label": ["c1", "c2", "c4", "c5"]},
        "employees": {
            "id": [1, 2, 3, 4],
            "manager": [None, 1, 1, 2],
            "name": ["root", "bea", "cy", "dean"],
        },
        "ticks": {
            "symbol": ["X", "X", "X", "Y", "Y"],
            "time": [0, 4, 8, 3, 6],
        },
        "quotes": {
            "symbol": ["X", "X", "Y"],
            "time": [2, 6, 1],
            "label": ["x2", "x6", "y1"],
        },
        "symbols": {"symbol": ["X", "Y"]},
    }
    try:
        definitions = []
        for name, columns in data.items():
            table = con.create_table(name, ibis.memtable(columns))
            definitions.append(TableDef(
                name, name,
                [ColumnDef(column, str(dtype)) for column, dtype in table.schema().items()],
            ))
        yield con, SchemaContext(definitions)
    finally:
        con.disconnect()


def run(doc: str, join_db) -> dict:
    con, schema = join_db
    ast = parse_query(doc, schema=schema, settled=True)
    assert ast.errors == []
    return execute(con, payload_from_ast(ast))


def row_bag(result: dict) -> Counter:
    return Counter(map(tuple, result["rows"]))


MATCHES = [
    ("a2x", "b2x"), ("a2x", "b2y"),
    ("a2y", "b2x"), ("a2y", "b2y"),
]


@pytest.mark.parametrize(("operator", "expected"), [
    ("==", MATCHES),
    ("?=", [*MATCHES, ("a1", None), ("a3", None)]),
    ("=?", [*MATCHES, (None, "b4")]),
    ("??", [*MATCHES, ("a1", None), ("a3", None), (None, "b4")]),
])
def test_join_types_preserve_the_correct_unmatched_side(join_db, operator, expected):
    result = run(
        f"\\from left_rows A\n\\from right_rows B\n\\join A.id {operator} B.id\n"
        "\\select A.label as a, B.label as b",
        join_db,
    )
    assert result["columns"] == ["a", "b"]
    assert row_bag(result) == Counter(expected)


@pytest.mark.parametrize(("operator", "expected"), [
    ("^=", [("a2x",), ("a2y",)]),
    ("!=", [("a1",), ("a3",)]),
])
def test_semi_and_anti_do_not_multiply_left_rows(join_db, operator, expected):
    result = run(
        f"\\from left_rows A\n\\from right_rows B\n\\join A.id {operator} B.id\n"
        "\\select A.label",
        join_db,
    )
    assert row_bag(result) == Counter(expected)


def test_first_edge_not_declaration_order_establishes_left_source(join_db):
    result = run(
        "\\from right_rows B\n\\from left_rows A\n\\join A.id ?= B.id\n"
        "\\select A.label as a, B.label as b",
        join_db,
    )
    assert row_bag(result) == Counter([*MATCHES, ("a1", None), ("a3", None)])


def test_block_edges_can_extend_any_connected_alias(join_db):
    result = run(
        "\\from left_rows A\n\\from right_rows B\n\\from third_rows C\n"
        "\\join\n  A.id == B.id\n  B.id ?= C.id\n"
        "\\select A.label as a, B.label as b, C.label as c",
        join_db,
    )
    assert row_bag(result) == Counter((a, b, "c2") for a, b in MATCHES)


def test_reversed_right_join_preserves_the_entire_accumulated_relation(join_db):
    result = run(
        "\\from left_rows A\n\\from third_rows C\n\\from right_rows B\n"
        "\\join A.id ?= C.id\n\\join B.id =? A.id\n"
        "\\select A.label as a, B.label as b, C.label as c",
        join_db,
    )
    expected = [(a, b, "c2") for a, b in MATCHES]
    expected += [("a1", None, "c1"), ("a3", None, None)]
    assert row_bag(result) == Counter(expected)


@pytest.mark.parametrize(("operator", "expected"), [
    ("^=", [("b2x",), ("b2y",)]),
    ("!=", [("b4",)]),
])
def test_reversed_semi_and_anti_return_the_written_left_relation(join_db, operator, expected):
    result = run(
        "\\from left_rows A\n\\from third_rows C\n\\from right_rows B\n"
        f"\\join A.id ?= C.id\n\\join B.id {operator} A.id\n"
        "\\select B.label",
        join_db,
    )
    assert row_bag(result) == Counter(expected)


def test_outer_join_after_inner_keeps_unmatched_alias_keys_null(join_db):
    result = run(
        "\\from left_rows A\n\\from right_rows B\n\\from third_rows C\n"
        "\\join A.id == B.id\n\\join B.id ?? C.id\n"
        "\\select A.id as a, B.id as b, C.id as c",
        join_db,
    )
    assert row_bag(result) == Counter([
        *[(2, 2, 2)] * 4, (None, None, 1), (None, None, 4), (None, None, 5),
    ])


@pytest.mark.parametrize(("operator", "bucket", "expected"), [
    ("^=", "y", [("b2y", "bea")]),
    ("!=", "x", [("b4", "dean")]),
])
def test_reversed_semi_and_anti_can_extend_and_filter_the_surviving_alias(
    join_db, operator, bucket, expected,
):
    result = run(
        "\\from left_rows A\n\\from third_rows C\n\\from right_rows B\n"
        "\\from employees D\n\\join A.id ?= C.id\n"
        f"\\join B.id {operator} A.id\n\\join B.id == D.id\n"
        f"\\where B.bucket = '{bucket}'\n\\select B.label, D.name",
        join_db,
    )
    assert row_bag(result) == Counter(expected)




@pytest.mark.parametrize("predicate", [
    "A.id ?= B.id and A.bucket ?= B.bucket left",
    "A.id ?= B.id and A.bucket = B.bucket left",
])
def test_composite_keys_filter_matches_without_losing_left_rows(join_db, predicate):
    result = run(
        f"\\from left_rows A\n\\from right_rows B\n\\join {predicate}\n"
        "\\select A.label as a, B.label as b",
        join_db,
    )
    assert row_bag(result) == Counter([
        ("a1", None), ("a2x", "b2x"), ("a2y", "b2y"), ("a3", None),
    ])


def test_backward_asof_uses_nearest_prior_key_within_equality_group(join_db):
    result = run(
        "\\from ticks A\n\\from quotes B\n"
        "\\join A.time @= B.time and A.symbol = B.symbol\n"
        "\\select A.symbol, A.time, B.label",
        join_db,
    )
    assert row_bag(result) == Counter([
        ("X", 0, None), ("X", 4, "x2"), ("X", 8, "x6"),
        ("Y", 3, "y1"), ("Y", 6, "y1"),
    ])


def test_reversed_asof_keeps_written_direction_and_unmatched_left_rows(join_db):
    result = run(
        "\\from ticks A\n\\from symbols C\n\\from quotes B\n"
        "\\join A.symbol == C.symbol\n"
        "\\join B.time @= A.time and B.symbol = A.symbol\n"
        "\\select B.label, A.time as tick_time",
        join_db,
    )
    assert row_bag(result) == Counter([("x2", 0), ("x6", 4), ("y1", None)])


def test_self_join_aliases_are_independent_relations(join_db):
    result = run(
        "\\from employees staff\n\\from employees boss\n"
        "\\join staff.manager ?= boss.id\n"
        "\\select staff.name as employee, boss.name as manager",
        join_db,
    )
    assert row_bag(result) == Counter([
        ("root", None), ("bea", "root"), ("cy", "root"), ("dean", "bea"),
    ])


def test_quoted_aliases_and_explicit_join_type_have_the_same_identity(join_db):
    quoted = run(
        '\\from left_rows A\n\\from right_rows B\n'
        '\\join "A".id inner join "B".id\n'
        '\\select A.label as a, B.label as b',
        join_db,
    )
    unquoted = run(
        '\\from left_rows A\n\\from right_rows B\n'
        '\\join A.id == B.id\n'
        '\\select A.label as a, B.label as b',
        join_db,
    )
    assert row_bag(quoted) == row_bag(unquoted) == Counter(MATCHES)


def test_cte_names_are_joinable_without_from_declarations(join_db):
    result = run(
        "\\with A\n  \\from left_rows\n  \\where id < 3\n"
        "\\with B\n  \\from right_rows\n  \\where bucket = 'x'\n"
        "\\join A.id ?= B.id\n\\select A.label as a, B.label as b",
        join_db,
    )
    assert row_bag(result) == Counter([
        ("a1", None), ("a2x", "b2x"), ("a2y", "b2x"),
    ])


def test_simple_join_inside_cte_body_keeps_aliases_local(join_db):
    result = run(
        "\\with matched\n"
        "  \\from left_rows A\n  \\from right_rows B\n"
        "  \\join\n    A.id == B.id and A.bucket = B.bucket\n"
        "  \\select A.id as id, B.label as label\n"
        "\\from matched A\n\\from third_rows B\n\\join A.id == B.id\n"
        "\\select A.label as a, B.label as b",
        join_db,
    )
    assert row_bag(result) == Counter([("b2x", "c2"), ("b2y", "c2")])


def test_legacy_source_join_remains_executable(join_db):
    result = run(
        "\\from left_rows A\n\\join right_rows B on A.id = B.id\n"
        "\\select A.label as a, B.label as b",
        join_db,
    )
    assert row_bag(result) == Counter(MATCHES)


@pytest.mark.parametrize("doc", [
    pytest.param(
        '\\from left_rows A\n\\from right_rows A\n\\join A.id == A.id',
        id="duplicate-alias",
    ),
    pytest.param(
        "\\from left_rows A\n\\from right_rows B\n\\join A.id == B.id\n\\join B.id == A.id",
        id="cycle",
    ),
    pytest.param(
        "\\from left_rows A\n\\from right_rows B\n\\from third_rows C\n"
        "\\from employees D\n\\join A.id == B.id\n\\join C.id == D.id",
        id="disconnected-edge",
    ),
    pytest.param(
        "\\from left_rows A\n\\from right_rows B\n\\from third_rows C\n\\join A.id == B.id",
        id="unused-explicit-source",
    ),
])
def test_invalid_alias_graphs_are_rejected(join_db, doc):
    _, schema = join_db
    assert parse_query(doc, schema=schema, settled=True).errors


@pytest.mark.parametrize("predicate", [
    pytest.param("A.id == missing.id", id="unknown-alias"),
    pytest.param("id == B.id", id="unqualified-left"),
    pytest.param("A.id == id", id="unqualified-right"),
    pytest.param("A. == B.id", id="missing-column"),
    pytest.param("A.id == B.id and", id="unfinished-composite"),
    pytest.param("A.id ?= B.id and A.bucket =? B.bucket", id="conflicting-operators"),
    pytest.param("A.id ?= B.id right", id="conflicting-trailing-type"),
    pytest.param("A.id == B.id and A.bucket = A.bucket", id="conflicting-alias-pair"),
])
def test_invalid_keys_and_conflicting_types_are_rejected(join_db, predicate):
    _, schema = join_db
    doc = f"\\from left_rows A\n\\from right_rows B\n\\join {predicate}"
    assert parse_query(doc, schema=schema, settled=True).errors


def test_multiline_error_points_to_the_actual_edge_line(join_db):
    _, schema = join_db
    doc = (
        "\\from left_rows A\n"
        "\\from right_rows B\n"
        "\\from third_rows C\n"
        "\\join\n"
        "  A.id == B.id\n"
        "  B.id == C.id\n"
        "  C.id == missing.id\n"
        "\\select A.label"
    )
    ast = parse_query(doc, schema=schema, settled=True)
    assert 7 in {error.line for error in ast.errors}
