"""JSON boundary semantics through documents, neutral Ibis graphs and real engines."""
from __future__ import annotations

import json

import ibis
import pytest

from d8r.engine import expression, tx
from d8r.engine.execute import execute, execute_remote, materialize
from d8r.query import parse_query, payload_from_ast


@pytest.fixture(scope="module", params=["duckdb", "sqlite"])
def json_con(request):
    con = getattr(ibis, request.param).connect()
    first = {
        "customer": {"name": 'a "quoted" dotted.name', "active": True},
        "items": [{"amount_cents": 199, "discount": 0.5}, {"amount_cents": -12}],
        "literal.dot": "literal", 'quote"dot.key': "quoted", "slash/~key": "slash",
        "nil": None, "numeric_text": "12", "flag": False, "object": {}, "real": 1.25,
        "0": "numeric-key",
    }
    second = {"customer": {"name": "other", "active": False}, "items": [{"amount_cents": 300}]}
    con.create_table("json_records", ibis.memtable(
        [(1, json.dumps(first), 'quote"dot.key', 0),
         (2, json.dumps(second), "missing", -1), (3, None, None, None)],
        schema={"id": "int64", "details_json": "string", "lookup_key": "string", "lookup_index": "int64"},
    ))
    if request.param == "duckdb":
        source = con.table("json_records")
        con.create_table("native_records", source.mutate(details_json=source.details_json.cast("json")))
    try:
        yield con
    finally:
        con.disconnect()


def payload(document):
    ast = parse_query(document, settled=True)
    assert ast.errors == []
    return payload_from_ast(ast)


def select(con, terms, table="json_records"):
    return execute(con, payload(f"\\from {table}\n\\select {terms}\n\\where id = 1"))


def test_literal_keys_indices_and_nested_json_keep_scalar_types(json_con):
    terms = (
        "json_text(details_json, 'customer', 'name') as name, "
        "json_int(details_json, 'items', 0, 'amount_cents') as cents, "
        "json_int(details_json, 'items', 1, 'amount_cents') as negative, "
        "json_float(details_json, 'items', 0, 'discount') as discount, "
        "json_bool(details_json, 'customer', 'active') as active, "
        "json_bool(details_json, 'flag') as flag, "
        "json_text(details_json, 'literal.dot') as dotted, "
        "json_text(details_json, 'slash/~key') as escaped, "
        "json_text(details_json, '0') as numeric_key, "
        "json_text(json_get(details_json, 'customer'), 'name') as composed"
    )
    expected = [['a "quoted" dotted.name', 199, -12, 0.5, True, False,
                 "literal", "slash", "numeric-key", 'a "quoted" dotted.name']]
    assert select(json_con, terms)["rows"] == expected
    if json_con.name == "duckdb":
        assert select(json_con, terms, "native_records")["rows"] == expected


def test_quoted_and_dynamic_object_keys_never_silently_report_missing(json_con):
    terms = "json_text(details_json, 'quote\"dot.key') as literal, json_text(details_json, lookup_key) as dynamic"
    if json_con.name == "sqlite":
        with pytest.raises(expression.PayloadError):
            select(json_con, "json_text(details_json, 'quote\"dot.key') as value")
        with pytest.raises(expression.PayloadError):
            select(json_con, "json_text(details_json, lookup_key) as value")
    else:
        assert select(json_con, terms)["rows"] == [["quoted", "quoted"]]


def test_typed_unwrap_does_not_coerce_other_json_types(json_con):
    result = select(json_con,
        "json_text(details_json, 'numeric_text') as text, "
        "json_int(details_json, 'numeric_text') as no_string_number, "
        "json_int(details_json, 'flag') as no_boolean_number, "
        "json_text(details_json, 'items', 0, 'amount_cents') as no_number_text, "
        "json_text(details_json, 'object') as no_object_text, "
        "json_int(details_json, 'real') as no_fraction_truncation, "
        "json_bool(details_json, 'numeric_text') as no_string_boolean, "
        "json_int('-42') as root_int, json_float('42') as root_float, "
        "json_bool('true') as root_bool, json_text('\"value\"') as root_text, "
        "json_text('[\"value\"]', '0') as no_string_array_index, "
        "json_text(null) as sql_null, json_text('null') as json_null"
    )
    assert result["rows"] == [["12", None, None, None, None, None, None,
                               -42, 42.0, True, "value", None, None, None]]


def test_json_null_missing_and_wrong_shape_are_sql_null_in_cte_filters(json_con):
    for key in ("nil", "missing"):
        result = execute(json_con, payload(
            "\\with extracted\n"
            "  \\from json_records\n"
            f"  \\select id, json_get(details_json, '{key}') as value\n"
            "\\from extracted\n\\where value is null\n\\select count(*) as n"
        ))
        assert result["rows"] == [[3]]
    result = select(json_con,
        "json_get(details_json, 'items', 9) as out_of_bounds, "
        "json_get(details_json, 'customer', 0) as object_not_array, "
        "json_get(details_json, 'numeric_text', 'nested') as scalar_not_object"
    )
    assert result["rows"] == [[None, None, None]]


def test_extraction_composes_with_filter_aggregate_and_arithmetic(json_con):
    result = execute(json_con, payload(
        "\\with extracted\n"
        "  \\from json_records\n"
        "  \\select json_bool(details_json, 'customer', 'active') as active, "
        "json_text(details_json, 'customer', 'name') as name, "
        "json_int(details_json, 'items', 0, 'amount_cents') as cents\n"
        "\\from extracted\n\\where active = true\n"
        "\\select name, sum(cents) / 100 as dollars, count(*) as n"
    ))
    assert result["rows"] == [['a "quoted" dotted.name', 1.99, 1]]


def test_dynamic_array_indices_reject_negative_and_null_values(json_con):
    result = execute(json_con, payload(
        "\\from json_records\n"
        "\\select id, json_int(details_json, 'items', lookup_index, 'amount_cents') as cents\n\\order id"
    ))
    assert result["rows"] == [[1, 199], [2, None], [3, None]]


def test_malformed_json_is_a_real_execution_error(json_con):
    with pytest.raises(Exception, match="(?i)json"):
        select(json_con, "json_text('{broken}', 'key') as value")


def test_materialization_and_remote_sql_share_real_sqlite_json_semantics(json_con):
    if json_con.name != "sqlite":
        pytest.skip("Remote D1 SQL and temp materialization use SQLite")
    request = payload(
        "\\from json_records\n\\select id, json_text(details_json, 'customer', 'name') as name, "
        "json_bool(details_json, 'customer', 'active') as active\n\\order id"
    )
    expected = [[1, 'a "quoted" dotted.name', True], [2, "other", False], [3, None, None]]
    assert materialize(json_con, request, "json_projection")["rows"] == expected
    handle = tx.temp_handle(json_con, "json_projection", expression.build(json_con, request).schema())
    assert execute(json_con, {"dataset": "json_projection", "orderBy": [{"target": "id"}]},
                   tables={"json_projection": handle})["rows"] == expected

    class LocalD1:
        def raw(self, sql):
            cursor = json_con.con.execute(sql)
            return [item[0] for item in cursor.description], cursor.fetchall()

    # The displayed dialect must not change which SQL runs over D1.
    result = execute_remote(LocalD1(), json_con, request, "postgres")
    assert result["rows"] == expected


@pytest.mark.parametrize("call", [
    {"fn": "json_get", "args": [{"column": "details_json"}]},
    {"fn": "json_text", "args": [{"literal": {"value": 123}}]},
    {"fn": "json_int", "args": [{"column": "details_json"}, {"literal": {"value": True}}]},
    {"fn": "json_float", "args": [{"column": "details_json"}, {"literal": {"value": 1.5}}]},
    {"fn": "json_bool", "args": [{"column": "details_json"}, {"literal": {"value": None}}]},
    {"fn": "json_text", "args": [{"column": "details_json"}, {"literal": {"value": -1}}]},
    {"fn": "json_text", "args": [{"column": "details_json", "path": "$.key"}]},
    {"fn": "json_text", "args": [{"column": "details_json"}], "path": "$.key"},
])
def test_raw_payload_cannot_bypass_json_argument_guards(json_con, call):
    with pytest.raises(expression.PayloadError):
        expression.build(json_con, {"dataset": "json_records", "select": [{"scalar": call}]})
