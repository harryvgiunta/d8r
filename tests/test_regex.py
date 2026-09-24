"""Regular expressions: the `~`/`!~` predicates and the two regex projections.

Patterns are the user's own, so the tests compare against the same matching done
in pandas (`str.contains`/`str.extract`/`str.replace` with regexes) rather than
against a spelling of the compiled SQL — and the refusals name the dtype, which
is the mistake a user actually makes.
"""

from __future__ import annotations

import pandas as pd
import pytest

from anyql.engine import expression
from anyql.engine.execute import execute
from anyql.query import parse_query, payload_from_ast


def messages(ast) -> list[str]:
    return [error.message for error in ast.errors]


def rows(doc: str, con) -> dict:
    ast = parse_query(doc)
    assert ast.errors == []
    return execute(con, payload_from_ast(ast))


# --- the shape the parser produces -------------------------------------------


def test_the_regex_operators_parse_in_where_and_case():
    ast = parse_query("\\from events\n\\where path ~ \"/p/[0-9]\"\n\\limit 2")
    assert ast.errors == []
    assert (ast.where.op, ast.where.column, ast.where.value) == ("~", "path", "/p/[0-9]")

    ast = parse_query("\\from events\n\\where path !~ \"^/q\"\n\\limit 2")
    assert ast.where.op == "!~"

    ast = parse_query("\\from events\n\\case p = when path ~ \"^/p\" then \"page\" else \"other\"\n\\limit 2")
    assert ast.errors == []
    assert ast.cases[0].whens[0].op == "~"


def test_regex_calls_parse_with_an_optional_group_and_a_replacement():
    ast = parse_query("\\from events\n\\select regexp_extract(path, \"/p/([0-9]+)\", 1)\n\\limit 2")
    assert ast.errors == []
    call = ast.select[0].regex
    assert (call.fn, call.arg, call.pattern, call.group) == ("regexp_extract", "path", "/p/([0-9]+)", 1)

    ast = parse_query("\\from events\n\\select regexp_extract(path, \"/p/[0-9]+\")\n\\limit 2")
    assert ast.select[0].regex.group is None  # the whole match

    ast = parse_query("\\from events\n\\select regexp_replace(path, \"/p/[0-9]+\", \"/page\") as where_to\n\\limit 2")
    assert ast.errors == []
    assert ast.select[0].regex.replacement == "/page"
    assert ast.select[0].alias == "where_to"


def test_regex_calls_take_the_derived_alias_and_refuse_a_frame():
    ast = parse_query("\\from events\n\\select regexp_extract(path, \"/p/[0-9]+\")\n\\limit 2")
    assert payload_from_ast(ast)["select"][0]["alias"] == "path_regexp_extract"

    ast = parse_query(
        "\\from events\n"
        "\\select regexp_extract(path, \"/p/[0-9]+\") over (order by timestamp)\n"
        "\\limit 2"
    )
    assert messages(ast) == [
        '"regexp_extract" is not a window function — use sum/avg/count/min/max'
    ]


def test_a_malformed_regex_call_names_its_shape():
    ast = parse_query("\\from events\n\\select regexp_extract(path)\n\\limit 2")
    assert messages(ast) == ["regexp_extract expects `<column>, <pattern>[, <group>]`"]

    ast = parse_query("\\from events\n\\select regexp_replace(path, \"/p/[0-9]+\")\n\\limit 2")
    assert messages(ast) == ["regexp_replace expects `<column>, <pattern>, <replacement>`"]

    ast = parse_query("\\from events\n\\select regexp_extract(user_id, \"/p/[0-9]+\")\n\\limit 2")
    assert ast.errors == []  # the dtype is the engine's business, not the parser's


# --- the rows it builds ------------------------------------------------------


def test_the_match_operator_selects_what_the_pattern_matches(con):
    result = rows(
        "\\from events\n\\select path\n\\where path ~ \"/p/[12]\"\n",
        con,
    )
    events = con.table("events").execute()
    want = events.loc[events["path"].str.contains("/p/[12]", regex=True), "path"]
    assert sorted(row[0] for row in result["rows"]) == sorted(want)


def test_the_negated_operator_is_the_complement(con):
    matched = rows("\\from events\n\\select path\n\\where path ~ \"/p/\"\n", con)
    unmatched = rows("\\from events\n\\select path\n\\where path !~ \"/p/\"\n", con)
    total = len(con.table("events").execute())
    assert len(matched["rows"]) + len(unmatched["rows"]) == total
    assert not ({row[0] for row in matched["rows"]} & {row[0] for row in unmatched["rows"]})


def test_extract_takes_the_group_it_is_given(con):
    result = rows(
        "\\from events\n"
        "\\select path\n"
        "\\select regexp_extract(path, \"/p/([0-9]+)\", 1) as part\n"
        "\\select regexp_extract(path, \"/p/[0-9]+\") as whole\n",
        con,
    )
    events = con.table("events").execute()
    part = events["path"].str.extract(r"/p/([0-9]+)", expand=False)
    # DuckDB's group 0 is the whole match; pandas spells that as one group around it.
    whole = events["path"].str.extract(r"(/p/[0-9]+)", expand=False)
    assert [row[1] for row in result["rows"]] == list(part.fillna(""))
    assert [row[2] for row in result["rows"]] == list(whole.fillna(""))


def test_replace_rewrites_every_match(con):
    result = rows(
        "\\from events\n"
        "\\select path, regexp_replace(path, \"/p/[0-9]+\", \"/page\") as rewritten\n",
        con,
    )
    events = con.table("events").execute()
    want = events["path"].str.replace(r"/p/[0-9]+", "/page", regex=True)
    assert [row[1] for row in result["rows"]] == list(want)


def test_a_pattern_with_a_comma_is_one_argument(con):
    # The comma sits inside the quoted pattern: `split_top` must not cut it.
    ast = parse_query("\\from events\n\\select regexp_replace(path, \"/p/[0-9]+\", \"/a,b\") as r\n\\limit 2")
    assert ast.errors == []
    assert ast.select[0].regex.replacement == "/a,b"


# --- the mistakes the engine names ------------------------------------------


def test_regex_needs_a_string_column(con):
    payload = payload_from_ast(parse_query("\\from events\n\\where user_id ~ \"1\"\n\\limit 2"))
    with pytest.raises(expression.PayloadError, match="`~` needs a string column"):
        expression.build(con, payload)

    payload = payload_from_ast(parse_query("\\from events\n\\select regexp_extract(user_id, \"1\")\n\\limit 2"))
    with pytest.raises(expression.PayloadError, match="regexp_extract needs a string column"):
        expression.build(con, payload)


# --- the SQL it renders ------------------------------------------------------


def test_the_operators_render_as_regex_functions(con):
    sql = expression.compile_sql(
        expression.build(
            con,
            payload_from_ast(
                parse_query(
                    "\\from events\n"
                    "\\select regexp_extract(path, \"/p/([0-9]+)\", 1) as part\n"
                    "\\select regexp_replace(path, \"/p/[0-9]+\", \"/page\") as rewritten\n"
                    "\\where path ~ \"/p/\"\n"
                    "\\limit 2"
                )
            ),
        )
    )
    assert "REGEXP_MATCHES" in sql
    assert "REGEXP_EXTRACT" in sql
    assert "REGEXP_REPLACE" in sql
