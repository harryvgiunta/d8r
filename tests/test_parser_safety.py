"""Regressions for parser availability and command-preserving expansion."""
from __future__ import annotations

import asyncio

import ibis
import pytest

from d8r.engine.execute import execute
from d8r.query import ColumnDef, FnDef, SchemaContext, TableDef, clause_line, parse_query, payload_from_ast
from d8r.query.parser import MAX_FUNCTION_DEPTH, MAX_FUNCTION_EXPANSIONS, MAX_NUMERIC_CHARS, MAX_WHERE_DEPTH, block_extent
from d8r.storage import MemoryStore
from d8r.tui.app import D8RApp
from d8r.tui.session import Session


def test_where_grouping_depth_is_bounded_not_crashed():
    def doc(nesting: int) -> str:
        return "\\from events\n\\where " + "(" * nesting + "user_id = 1" + ")" * nesting

    allowed = parse_query(doc(MAX_WHERE_DEPTH), settled=True)
    assert allowed.errors == []
    refused = parse_query(doc(MAX_WHERE_DEPTH + 1), settled=True)
    assert [error.line for error in refused.errors] == [2]
    assert "nested too deeply" in refused.errors[0].message



@pytest.mark.parametrize("clause", [
    "\\limit {number}",
    "\\select {number} as value",
    "\\select regexp_extract(path, 'a', {number})",
    "\\select left(path, {number})",
])
def test_oversized_numbers_are_diagnostics_not_exceptions(clause):
    doc = "\\from events\n" + clause.format(number="9" * 4500)
    assert parse_query(doc).errors == []  # Still editing the last line.
    settled = parse_query(doc, settled=True)
    assert [error.line for error in settled.errors] == [2]
    assert "too large" in settled.errors[0].message
    session = Session()
    assert "too large" in session.run(doc).error
    sql, error = session.compile(doc)
    assert sql is None and "too large" in error
    assert session.history == []


def test_numeric_size_boundary_does_not_coerce_quoted_digits():
    boundary = "0" * (MAX_NUMERIC_CHARS - 1) + "1"
    digits = "9" * (MAX_NUMERIC_CHARS + 1)
    ast = parse_query(f"\\from events\n\\select {boundary} as n, '{digits}' as text\n\\limit {boundary}", settled=True)
    assert ast.errors == []
    assert ast.limit == 1
    assert [item.literal.value for item in ast.select] == [1, digits]
    assert "too large" in parse_query("\\limit " + digits, settled=True).errors[0].message


def test_function_replacement_cycle_preserves_memory_and_saved_body():
    session = Session()
    body = "\\from events\n\\select user_id\n\\limit @x"
    session.save_fn("b", "x", body, "")
    session.save_fn("a", "x", "\\from b(@x)", "")
    path = MemoryStore().path
    original = path.read_bytes()
    with pytest.raises(ValueError, match="recursive function call"):
        session.save_fn("b", "x", "\\from a(@x)", "")
    assert path.read_bytes() == original
    assert session.fns["b"].body == body
    restored = Session()
    result = restored.run("\\from a(1)")
    assert result.error == "" and result.total == 1


def test_new_self_recursive_function_is_rejected_before_first_save():
    session = Session()
    with pytest.raises(ValueError, match="recursive function call"):
        session.save_fn("self_call", "", "\\from self_call()", "")
    assert not MemoryStore().path.exists()
    assert session.fns == {}


def test_expansion_depth_survives_inline_subqueries_and_allows_siblings():
    functions = [FnDef("leaf", body="\\from events\n\\select user_id")]
    for index in range(MAX_FUNCTION_DEPTH):
        target = "leaf" if index == 0 else f"chain{index - 1}"
        functions.append(FnDef(f"chain{index}", body=f"\\from (\\from {target}()) as nested"))
    schema = SchemaContext(fns=functions)
    allowed = parse_query(f"\\from chain{MAX_FUNCTION_DEPTH - 2}()", schema=schema, settled=True)
    assert allowed.errors == []
    refused = parse_query(f"\\from chain{MAX_FUNCTION_DEPTH - 1}()", schema=schema, settled=True)
    assert "nested calls" in refused.errors[0].message
    siblings = parse_query("\\from leaf()\n\\union all leaf()", schema=schema, settled=True)
    assert siblings.errors == []
    assert siblings.from_.body.select[0].column == siblings.set_ops[0].body.select[0].column == "user_id"


def test_branching_expansion_budget_is_shared_and_save_matches_call():
    session = Session()
    session.save_fn("leaf", "", "\\from events\\limit 1", "")
    body = "\\from leaf()" + "\\union all leaf()" * (MAX_FUNCTION_EXPANSIONS - 2)
    session.save_fn("branches", "", body, "")
    assert parse_query("\\from branches()", schema=session.schema, settled=True).errors == []
    with pytest.raises(ValueError, match="function expansions"):
        session.save_fn("branches", "", body + "\\union all leaf()", "")
    schema = SchemaContext(fns=[*session.schema.fns, FnDef("extra", body="\\from branches()")])
    assert "function expansions" in parse_query("\\from extra()", schema=schema, settled=True).errors[0].message


def install_legacy_cycle():
    MemoryStore().save([
        {"name": "a", "params": ["x"], "body": "\\from b(@x)", "description": ""},
        {"name": "b", "params": ["x"], "body": "\\from a(@x)", "description": ""},
    ], [])


def test_legacy_cycles_are_recoverable_without_erasing_saved_work():
    install_legacy_cycle()
    session = Session()
    assert "recursive function call" in session.run("\\from b(1)").error
    session.save_fn("b", "x", "\\from events\n\\select user_id\n\\limit @x", "repaired")
    restored = Session()
    result = restored.run("\\from a(2)")
    assert result.error == "" and result.total == 2


@pytest.mark.parametrize("column", ["range", "rows", "order"])
def test_window_keywords_remain_columns_and_produce_partition_totals(con, column):
    table = ibis.memtable({column: [1, 1, 2], "amount": [2, 3, 7]})
    doc = f"\\from named n\n\\select n.{column}, sum(amount) over (partition by n.{column}) as total\n\\order n.{column}"
    ast = parse_query(doc, settled=True)
    assert ast.errors == []
    result = execute(con, payload_from_ast(ast), tables={"named": table})
    assert result["rows"] == [[1, 5], [1, 5], [2, 7]]
    ordered = parse_query(
        f"\\from named\n\\select sum(amount) over (order by {column} rows between unbounded preceding and current row) as total",
        settled=True,
    )
    assert ordered.errors == []
    assert ordered.select[0].window.order.column == column
    assert ordered.select[0].window.frame.start == "unbounded preceding"


@pytest.mark.parametrize("call", ["regexp_extract({ref}, 'a')", "regexp_replace({ref}, 'a', 'b')"])
def test_regex_column_arguments_follow_alias_validation(call):
    schema = SchemaContext(tables=[TableDef("events", columns=[ColumnDef("path", "string")])])
    doc = "\\from events e\n\\select " + call.format(ref="bogus.path")
    errors = parse_query(doc, schema=schema, settled=True).errors
    assert len(errors) == 1 and errors[0].line == 2
    assert '"bogus" is not an open table: e' in errors[0].message
    assert parse_query(doc.replace("bogus.path", "e.path"), schema=schema, settled=True).errors == []
    assert parse_query(doc, schema=schema).errors == []
    assert parse_query(doc, settled=True).errors == []


def test_blank_runs_preserve_cte_and_clause_boundaries():
    lines = ["\\with body", "  \\from events", *([""] * 30), "  \\select user_id", "", "\\from body", "\\select user_id"]
    end = lines.index("\\from body") - 1
    assert block_extent(lines, 0) == end
    doc = "\n".join(lines)
    ast = parse_query(doc, settled=True)
    assert ast.errors == []
    assert ast.with_[0].body.select[0].line == end
    assert clause_line(doc, 3, "select") == end
    assert clause_line(doc, len(lines), "select") == len(lines)
    assert block_extent(["\\with body", "  \\from events", "", ""], 0) == 2


def test_function_arguments_cannot_change_following_commands():
    schema = SchemaContext(fns=[FnDef("pat", params=["value"], body="\\from events\n\\where path = @value\n\\select path\n\\limit 1")])
    for argument, value in [("'(x'", "(x"), ('"O\'Reilly"', "O'Reilly"), ("'a,b'", "a,b")]:
        ast = parse_query(f"\\from pat({argument})", schema=schema, settled=True)
        assert ast.errors == []
        body = ast.from_.body
        assert body.where.value == value
        assert body.select[0].column == "path" and body.limit == 1
    for argument in ["'unterminated", "(x", "x)", ","]:
        assert parse_query(f"\\from pat({argument})", schema=schema, settled=True).errors


def test_malformed_function_templates_cannot_swallow_following_commands():
    session = Session()
    for value in ["@value)", "'@value"]:
        body = f"\\from events\n\\where path = {value}\n\\select path\n\\limit 1"
        with pytest.raises(ValueError, match="unbalanced"):
            session.save_fn("broken", "value", body, "")
        # A legacy definition is checked on use too, without a save-time assumption.
        schema = SchemaContext(fns=[FnDef("broken", params=["value"], body=body)])
        errors = parse_query("\\from broken('x')", schema=schema, settled=True).errors
        assert "unbalanced" in errors[0].message


def test_completion_survives_oversized_paste_and_persisted_cycles():
    install_legacy_cycle()
    app = D8RApp()

    async def drive():
        async with app.run_test(size=(120, 40)) as pilot:
            for document in ["\\from events\n\\limit " + "9" * 4500 + "\n\\select ", "\\from b(1)\n\\select "]:
                app.editor.load_text(document)
                app.editor.cursor_location = (2 if "\\limit" in document else 1, len("\\select "))
                await pilot.pause()
            app.editor.load_text("\\from events\n\\select user_id\n\\limit 1")
            app.editor.cursor_location = (2, len("\\limit 1"))
            await pilot.press("f5")
            for _ in range(100):
                await pilot.pause()
                if not app.session.busy:
                    break
            assert app.query_one("#results-table").row_count == 1
            assert app.session.history[-1].rows == 1

    asyncio.run(drive())
