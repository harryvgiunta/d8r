"""Arithmetic completion preserves operands and produces runnable query text."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from textual.widgets import Input, TextArea

from d8r.engine.execute import execute
from d8r.query import parse_query, payload_from_ast
from d8r.query.schema import ColumnDef, SchemaContext
from d8r.tui.app import D8RApp
from d8r.tui.palette import view_for
from d8r.tui.session import Session


@pytest.fixture
def session():
    return Session()


def complete(session, marked: str, label: str) -> str:
    """Accept a palette entry at | using the same replacement span as the widget."""
    caret = marked.index("|")
    line = marked.replace("|", "", 1)
    view = view_for(session, "\\from events\n" + line, line, caret)
    assert view is not None
    entry = next(entry for entry in view.entries if entry.label == label)
    end = caret if view.end is None else view.end
    return line[:view.start] + entry.insert + line[end:]


def query_rows(session, con, line):
    ast = parse_query("\\from events\n" + line, schema=session.schema)
    assert ast.errors == []
    return execute(con, payload_from_ast(ast))["rows"]


def test_divisor_completion_keeps_left_operand_and_executes(session, con):
    line = complete(session, "\\select amount / us|", "user_id")
    assert line == "\\select amount / user_id "
    rows = query_rows(session, con, line + "as ratio, amount, user_id")
    for ratio, amount, user_id in rows:
        assert ratio == (pytest.approx(amount / user_id) if user_id else None)


def test_grouped_unary_completion_preserves_enclosing_parentheses(session, con):
    line = complete(session, "\\select -(2 + us|) * 3 as value", "user_id")
    assert line == "\\select -(2 + user_id ) * 3 as value"
    rows = query_rows(session, con, line + ", user_id")
    assert all(value == -(2 + user_id) * 3 for value, user_id in rows)


def test_scalar_arithmetic_completion_does_not_close_inner_operand(session, con):
    line = complete(session, "\\select string((us| + 1)) as value", "user_id")
    assert line == "\\select string((user_id + 1)) as value"
    rows = query_rows(session, con, line + ", user_id")
    assert all(value == str(user_id + 1) for value, user_id in rows)
    # An ungrouped arithmetic argument stays open too, while a plain unary
    # scalar argument retains its existing auto-close behavior.
    assert complete(session, "\\select string(1 + us|", "user_id") == "\\select string(1 + user_id"
    assert complete(session, "\\select string(us|", "user_id") == "\\select string(user_id)"
    assert complete(session, "\\select string(us| + 1)", "user_id") == "\\select string(user_id + 1)"
    line = complete(session, "\\select 1 / coalesce(us|, 1) as value", "user_id")
    assert line == "\\select 1 / coalesce(user_id, 1) as value"
    rows = query_rows(session, con, line + ", user_id")
    assert all(value == (pytest.approx(1 / user_id) if user_id else None) for value, user_id in rows)


def test_nested_numeric_call_completes_its_own_string_argument(session, con):
    line = complete(session, "\\select 2 * length(pa|", "path")
    assert line == "\\select 2 * length(path)"
    rows = query_rows(session, con, line + " as size, path")
    assert all(size == 2 * len(path) for size, path in rows)


def test_quoted_operators_and_scientific_exponents_are_not_boundaries(session, con):
    line = complete(session, "\\select length('it''s + / (text)') + 1e-3 * us|", "user_id")
    assert line == "\\select length('it''s + / (text)') + 1e-3 * user_id "
    rows = query_rows(session, con, line + "as value, user_id")
    assert all(value == pytest.approx(len("it's + / (text)") + 0.001 * user_id) for value, user_id in rows)
    for line in ("\\select 1e-", "\\select 1E+us", "\\select length('open + us", '\\select "open / us'):
        assert view_for(session, "\\from events\n" + line, line, len(line)) is None


def test_arithmetic_menu_filters_result_types_and_keeps_aggregate_leaves(session):
    tables = tuple(
        replace(table, columns=(*table.columns, ColumnDef("flag", "boolean"),
                                ColumnDef("required_count", "!int64"), ColumnDef("required_amount", "!decimal(12, 2)")))
        if table.name == "events" else table
        for table in session.schema.tables
    )
    session.schema = SchemaContext(tables, session.schema.capabilities, session.schema.fns)
    line = "\\select amount / "
    view = view_for(session, "\\from events\n" + line, line, len(line))
    assert {"amount", "user_id", "required_count", "required_amount", "length", "coalesce", "nullif", "sum", "count"} <= set(view.labels)
    assert not {"path", "event_type", "flag", "upper", "contains", "year", "rank", "row_number"} & set(view.labels)
    # A numeric argument to a scalar does not suddenly permit aggregate leaves.
    line = "\\select substr(path, 1 + "
    view = view_for(session, "\\from events\n" + line, line, len(line))
    assert {"user_id", "required_count", "length"} <= set(view.labels)
    assert not {"path", "flag", "sum", "count", "upper"} & set(view.labels)
    line = "\\select amount / coalesce("
    view = view_for(session, "\\from events\n" + line, line, len(line))
    assert "amount" in view.labels and "path" not in view.labels


def test_aggregate_leaf_arguments_remain_columns_and_preserve_suffix(session, con):
    line = complete(session, "\\select 2 * sum(am|) as total", "amount")
    assert line == "\\select 2 * sum(amount) as total"
    rows = query_rows(session, con, line)
    expected = con.table("events").execute().amount.sum() * 2
    assert rows[0][0] == pytest.approx(expected)
    line = "\\select 2 * min("
    view = view_for(session, "\\from events\n" + line, line, len(line))
    assert "amount" in view.labels and "path" not in view.labels
    for line in (
        "\\select sum(amount / us", "\\select sum((us", "\\select string(1 + sum(us",
        "\\select amount + rank(", "\\select amount + year(",
    ):
        assert view_for(session, "\\from events\n" + line, line, len(line)) is None


def test_arithmetic_parameter_completion_follows_unsaved_function_declaration():
    async def scenario():
        app = D8RApp()
        async with app.run_test(size=(140, 45)) as pilot:
            app.action_fn()
            await pilot.pause()
            screen = app.screen
            params = screen.query_one("#fn-params", Input)
            body = screen.query_one("#fn-body", TextArea)
            palette = screen.query_one("#fn-palette")
            params.value = "old_scale"
            body.load_text("\\from events\n\\select amount / (@sc_stale + 1)")
            body.cursor_location = (1, len("\\select amount / (@sc"))
            body.focus()
            params.value = "scale"
            await pilot.pause()
            assert palette.view.labels == ["@scale"]
            await pilot.press("enter")
            assert body.text == "\\from events\n\\select amount / (@scale + 1)"

    asyncio.run(scenario())
