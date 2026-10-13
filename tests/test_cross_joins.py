"""Cartesian joins retain independent source identities and reject join predicates."""
from __future__ import annotations

from collections import Counter

import ibis
import pytest

from d8r.engine.execute import execute
from d8r.engine.expression import PayloadError, build, validate_schema
from d8r.query import ColumnDef, FnDef, SchemaContext, TableDef, parse_query, payload_from_ast


@pytest.fixture(params=["duckdb", "sqlite"])
def cross_db(request):
    con = getattr(ibis, request.param).connect()
    data = {
        "left_rows": {"id": [1, 2]},
        "right_rows": {"id": [10, 20, 30]},
        "third_rows": {"id": [20, 30], "label": ["twenty", "thirty"]},
        "cross": {"id": [2]},
    }
    try:
        definitions = []
        for name, columns in data.items():
            table = con.create_table(name, ibis.memtable(columns))
            definitions.append(TableDef(
                name, name,
                [ColumnDef(column, str(dtype)) for column, dtype in table.schema().items()],
            ))
        con.create_table("empty_rows", schema={"id": "int64"})
        definitions.append(TableDef("empty_rows", "empty_rows", [ColumnDef("id", "int64")]))
        yield con, SchemaContext(definitions, fns=(
            FnDef("above", ("minimum",), "\\from right_rows\n\\where id > @minimum\n\\select id"),
            FnDef("bad", (), "\\from right_rows\n\\select missing_field"),
        ))
    finally:
        con.disconnect()


def rows(doc, cross_db):
    con, schema = cross_db
    ast = parse_query(doc, schema=schema, settled=True)
    assert ast.errors == []
    tables = {table.name: con.table(table.name) for table in schema.tables}
    return Counter(map(tuple, execute(con, payload_from_ast(ast), tables=tables)["rows"]))


def test_cartesian_product_retains_all_six_pairs(cross_db):
    assert rows(
        "\\from left_rows A\n\\join CrOsS right_rows as B\n\\select A.id as a, B.id as b",
        cross_db,
    ) == Counter((a, b) for a in (1, 2) for b in (10, 20, 30))


def test_repeated_self_cross_sources_are_independent(cross_db):
    assert rows(
        "\\from left_rows A\n\\join cross left_rows B\n\\join cross left_rows C\n"
        "\\select A.id as a, B.id as b, C.id as c", cross_db,
    ) == Counter((a, b, c) for a in (1, 2) for b in (1, 2) for c in (1, 2))


@pytest.mark.parametrize("doc", [
    "\\from empty_rows A\n\\join cross right_rows B",
    "\\from left_rows A\n\\join cross empty_rows B",
])
def test_empty_operand_annihilates_product(cross_db, doc):
    assert rows(doc + "\n\\select A.id as a, B.id as b", cross_db) == Counter()


def test_filtered_product_groups_the_cross_alias(cross_db):
    assert rows(
        "\\from left_rows A\n\\join cross right_rows B\n\\where B.id > 10\n"
        "\\group A.id\n\\select A.id, sum(B.id) as total, count(*) as n",
        cross_db,
    ) == Counter([(1, 50, 2), (2, 50, 2)])


@pytest.mark.parametrize("tail", [
    "\\join third_rows C on B.id = C.id",
    "\\from third_rows C\n\\join B.id == C.id",
])
def test_following_source_and_alias_joins_can_use_cross_alias(cross_db, tail):
    assert rows(
        "\\from left_rows A\n\\join cross right_rows B\n" + tail
        + "\n\\select A.id as a, B.id as b, C.label as label", cross_db,
    ) == Counter((a, b, label) for a in (1, 2) for b, label in ((20, "twenty"), (30, "thirty")))


def test_cross_can_follow_an_alias_join(cross_db):
    assert rows(
        "\\from right_rows B\n\\from third_rows C\n\\join B.id == C.id\n"
        "\\join cross left_rows A\n\\select A.id as a, C.label as label", cross_db,
    ) == Counter((a, label) for a in (1, 2) for label in ("twenty", "thirty"))


@pytest.mark.parametrize("prefix,source", [
    ("", "( \\from right_rows \\where id > 10 ) B"),
    ("\\with chosen\n  \\from right_rows\n  \\where id > 10\n", "chosen B"),
    ("", "above(10) B"),
])
def test_cross_reuses_subquery_cte_and_function_sources(cross_db, prefix, source):
    assert rows(
        prefix + "\\from left_rows A\n\\join cross " + source
        + "\n\\select A.id as a, B.id as b", cross_db,
    ) == Counter((a, b) for a in (1, 2) for b in (20, 30))


def test_unaliased_and_qualified_sources_keep_existing_identifiers(cross_db):
    con, schema = cross_db
    table = con.table("right_rows")
    qualified = TableDef("main.right_rows", "main.right_rows", [ColumnDef("id", "int64")])
    schema = SchemaContext((*schema.tables, qualified))
    ast = parse_query(
        "\\from left_rows\n\\join cross main.right_rows\n"
        "\\select left_rows.id as a, main.right_rows.id as b", schema=schema, settled=True,
    )
    assert ast.errors == []
    result = build(con, payload_from_ast(ast), tables={"main.right_rows": table}).execute()
    assert Counter(map(tuple, result.itertuples(index=False, name=None))) == Counter(
        (a, b) for a in (1, 2) for b in (10, 20, 30)
    )


@pytest.mark.parametrize("source", ["cross", "cross B", "cross as B", '"cross" B'])
def test_ordinary_table_named_cross_with_on_is_not_reinterpreted(cross_db, source):
    assert rows(
        "\\from left_rows A\n\\join " + source + " on id\n\\select A.id", cross_db,
    ) == Counter([(2,)])


@pytest.mark.parametrize("doc", [
    "\\join cross right_rows B",
    "\\from left_rows A\n\\join cross",
    "\\from left_rows A\n\\join cross right_rows B on A.id = B.id",
    "\\from left_rows A\n\\join cross absent B",
    "\\from left_rows A\n\\join cross right_rows A",
    "\\from left_rows A\n\\join cross ( \\from right_rows )",
    "\\from left_rows A\n\\join cross ( \\from absent ) B",
    "\\from left_rows A\n\\join cross above() B",
    "\\from left_rows A\n\\join cross above(10) A",
    "\\from left_rows A\n\\join cross lateral ( \\from right_rows ) B",
])
def test_invalid_cross_sources_are_parser_errors(cross_db, doc):
    _, schema = cross_db
    assert parse_query(doc, schema=schema, settled=True).errors


@pytest.mark.parametrize("fields", [
    {"left": "id"},
    {"right": "id"},
    {"left": None},
    {"keys": [["id", "id"]]},
    {"lateral": True},
    {"reverse": True},
    {"dataset": "", "alias": "B"},
    {"on": "id"},
])
def test_invalid_cross_payloads_raise_payload_errors(cross_db, fields):
    con, _ = cross_db
    spec = {"kind": "cross", "dataset": "right_rows", "left": "", "right": "", **fields}
    with pytest.raises(PayloadError):
        build(con, {"dataset": "left_rows", "joins": [spec]})


@pytest.mark.parametrize("source,selection", [
    ("( \\from right_rows \\select id as renamed ) B", "B.id"),
    ("bad() B", "B.missing_field"),
    ("( \\from right_rows \\where id = A.id ) B", "B.id"),
])
def test_schema_validation_rejects_missing_fields_and_correlation(cross_db, source, selection):
    _, schema = cross_db
    ast = parse_query(
        "\\from left_rows A\n\\join cross " + source + "\n\\select " + selection,
        schema=schema, settled=True,
    )
    with pytest.raises(PayloadError):
        validate_schema(ast, schema)


def test_correlated_lateral_remains_per_left_row(cross_db):
    assert rows(
        "\\from left_rows A\n"
        "\\join lateral ( \\from left_rows L \\where L.id = A.id ) B\n"
        "\\select A.id as a, B.id as b", cross_db,
    ) == Counter([(1, 1), (2, 2)])
