"""Scalar strings through the language, payload compiler, and real backends."""

from __future__ import annotations

import sqlite3

import pytest

from anyql.engine import add_sqlite_source, expression
from anyql.engine.execute import execute
from anyql.query import parse_query, payload_from_ast


def run(doc: str, con, **kwargs) -> dict:
    ast = parse_query(doc)
    assert ast.errors == []
    return execute(con, payload_from_ast(ast), **kwargs)


def test_nested_calls_keep_quoting_conversion_and_default_names(con):
    events = con.table("events").execute()
    # Ties on user_id may have different paths; order by both derived outputs.
    result = run(
        "\\from events e\n"
        "\\select concat(upper('it''s, as over (x)'), ':', string(e.user_id)) as label, "
        "concat_ws('|', lower('AB'), '', substr(e.path, 1)) as joined, "
        "string(12), string(true), string(1.5)\n"
        "\\order label\n\\order joined\n\\limit 2",
        con,
    )
    expected = sorted(
        [[f"IT'S, AS OVER (X):{row.user_id}", f"ab||{row.path[1:]}", "12", "true", "1.5"]
         for row in events.itertuples()]
    )[:2]
    assert result["columns"] == ["label", "joined", "string(12)", "string(true)", "string(1.5)"]
    assert result["rows"] == expected


def test_catalog_operations_and_zero_based_positions(con):
    result = run(
        "\\from events\n"
        "\\select capitalize('hELLO'), length('abc'), strip(' x '), lstrip(' x '), "
        "rstrip(' x '), substr('abcdef', 0, 2), substr('abcdef', 2), "
        "left('abcdef', 2), right('abcdef', 2), replace('ababa', 'a', 'X'), "
        "contains('abc', 'b'), startswith('abc', 'ab'), endswith('abc', 'bc'), "
        "repeat('ab', 2), reverse('abc'), lpad('a', 3), rpad('a', 3, '-'), "
        "find('ababa', 'ba'), find('ababa', 'ba', 2), find('abc', 'z')\n"
        "\\limit 1",
        con,
    )
    assert result["rows"] == [[
        "Hello", 3, "x", "x ", " x", "ab", "cdef", "ab", "ef", "XbXbX",
        True, True, True, "abab", "cba", "  a", "a--", 1, 3, -1,
    ]]


def test_typed_nulls_empty_strings_and_empty_input(con):
    projection = (
        "\\from events\n"
        "\\select lower(null) as missing, substr('abc', null) as missing_part, "
        "string(null) as missing_text, concat_ws('|', 'a', null) as joined, "
        "concat('', '') as empty, concat_ws('|', 'only') as single"
    )
    assert run(projection + "\n\\limit 1", con)["rows"] == [[None, None, None, "a", "", "only"]]
    assert run(projection + "\n\\where user_id < 0", con)["rows"] == []
    result = run(
        "\\from events\n\\select count(user_id) as n, "
        "concat(upper('ok'), string(1)) as marker, lower(null) as missing\n"
        "\\where user_id < 0",
        con,
    )
    assert result["columns"] == ["n", "marker", "missing"]
    assert result["rows"] == [[0, "OK1", None]]


def test_column_calls_group_implicitly_but_scalar_calls_do_not(con):
    result = run(
        "\\from events\n"
        "\\select upper(event_type) as kind, count(user_id) as n, "
        "concat('o', 'k') as marker\n\\order kind",
        con,
    )
    events = con.table("events").execute()
    expected = sorted([[kind.upper(), int(n), "ok"] for kind, n in events.groupby("event_type").size().items()])
    assert result["columns"] == ["kind", "n", "marker"]
    assert result["rows"] == expected
    explicitly_grouped = run(
        "\\from events\n\\select event_type, count(user_id) as n, upper('ok') as marker\n"
        "\\group event_type\n\\order event_type",
        con,
    )
    assert explicitly_grouped["rows"] == [[kind.lower(), n, "OK"] for kind, n, _ in expected]


def test_like_needs_a_string_column(con):
    """`like` compares text; a numeric column is the user's mistake and is
    named as one — the same shape as the regex guard."""
    payload = payload_from_ast(parse_query("\\from events\n\\where amount like \"5\"\n\\limit 2"))
    with pytest.raises(expression.PayloadError, match="`like` needs a string column"):
        expression.build(con, payload)


@pytest.mark.parametrize("call", [
    "lower(user_id)",
    "concat(path, 1)",
    "substr(path, true)",
    "repeat(path, 1.5)",
    "upper(length(path))",
])
def test_explicit_conversion_is_required_and_booleans_are_not_integers(con, call):
    ast = parse_query(f"\\from events\n\\select {call}")
    assert ast.errors == []
    with pytest.raises(expression.PayloadError):
        expression.build(con, payload_from_ast(ast))


@pytest.mark.parametrize("scalar", [
    {"fn": "lower", "args": []},
    {"fn": "lower", "args": [{"column": "path"}, {"column": "path"}]},
    {"fn": "execute", "args": [{"column": "path"}]},
    {"fn": [], "args": []},
    {"fn": "lower", "args": {"column": "path"}},
    {"fn": "lower", "args": ["path"]},
    {"fn": "lower", "args": [{"column": "path", "literal": {"value": "x"}}]},
    {"fn": "lower", "args": [{"literal": {}}]},
    {"fn": "lower", "args": [{"literal": {"value": []}}]},
    {"fn": "lower", "args": [{"aggregate": {"fn": "count", "arg": "path"}}]},
    {"fn": "lower", "args": [{"fn": "upper", "args": [{"column": "path"}], "window": {}}]},
])
def test_malformed_payloads_are_user_errors(con, scalar):
    with pytest.raises(expression.PayloadError):
        expression.build(con, {"dataset": "events", "select": [{"scalar": scalar}]})


def test_scalar_projection_participates_in_star_and_rejects_windows(con):
    result = run("\\from events\n\\select upper(event_type) as event_type, *\n\\limit 1", con)
    event = con.table("events").execute().iloc[0]
    assert result["columns"] == ["event_type", "timestamp", "user_id", "amount", "path"]
    assert result["rows"][0][0] == event.event_type.upper()
    scalar = {"fn": "lower", "args": [{"column": "path"}]}
    for extra in ({"star": True}, {"window": {"order": {"column": "path"}}}, {"column": "path"}):
        with pytest.raises(expression.PayloadError):
            expression.build(con, {"dataset": "events", "select": [{"scalar": scalar, **extra}]})
    result = execute(con, {"dataset": "events", "select": [{"scalar": scalar}], "limit": 1})
    assert result["columns"] == ["lower"]
    assert result["rows"] == [[event.path.lower()]]


def test_supported_calls_execute_on_local_sqlite(tmp_path):
    path = tmp_path / "strings.sqlite"
    with sqlite3.connect(path) as raw:
        raw.execute("create table words (id integer, word text)")
        raw.executemany("insert into words values (?, ?)", [(1, " AbC "), (2, ""), (3, None)])
    source = add_sqlite_source("strings", str(path))
    try:
        result = run(
            "\\from words\n\\select id, lower(strip(word)) as normalized, "
            "substr(word, 1, 2) as part, string(id) as text_id, "
            "replace(word, 'b', 'x') as replaced\n\\order id",
            source.con,
            tables={name: dataset["table"] for name, dataset in source.datasets.items()},
        )
        assert result["rows"] == [
            [1, "abc", "Ab", "1", " AxC "],
            [2, "", "", "2", ""],
            [3, None, None, "3", None],
        ]
    finally:
        source.con.disconnect()


@pytest.mark.parametrize("call", [
    "concat(path,, 'x')",
    "upper(path, 'x')",
    "concat(path, unknown(path))",
    "lower(path) over (order by user_id)",
])
def test_malformed_scalar_calls_error_only_after_the_typing_line(call):
    doc = f"\\from events\n\\select {call}"
    assert parse_query(doc).errors == []
    settled = parse_query(doc + "\n\\limit 1")
    assert [error.line for error in settled.errors] == [2]


def test_nested_scalar_references_obey_table_aliases():
    from anyql.query import ColumnDef, SchemaContext, TableDef

    schema = SchemaContext([TableDef("events", columns=[ColumnDef("path", "string")])])
    doc = "\\from events e\n\\select concat(upper(events.path), 'x')\n\\limit 1"
    errors = parse_query(doc, schema=schema).errors
    assert len(errors) == 1 and errors[0].line == 2
    assert parse_query(doc.replace("upper(events.path)", "upper(e.path)"), schema=schema).errors == []
