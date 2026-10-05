"""Predicate completion and accept-only Tab regressions."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from textual.widgets import DataTable

from d8r.query import ForeignKey
from d8r.tui.app import D8RApp
from d8r.tui.palette import view_for
from d8r.tui.session import Session


@pytest.mark.parametrize("predicate", [
    "amount > 1 and ev",
    "amount > 1 OR ev",
    "(amount > 1 AND (user_id > 2 OR (ev",
    "(amount > 1) AND (ev",
    "amount BETWEEN 1 AND 10 AND ev",
    "event_type = 'and OR (between)' AND ev",
    'event_type = "it\'s AND OR" OR ev',
])
def test_where_joiners_complete_the_current_condition(predicate, sources):
    session = Session(sources)
    line = "\\where " + predicate
    view = view_for(session, "\\from events\n" + line, line, len(line))
    assert view is not None
    assert view.labels == ["event_type"]
    assert line[view.start:] == "ev"


def test_between_connector_keeps_value_completion(sources):
    session = Session(sources)
    line = "\\where (event_type BETWEEN 'click' AND 'pur"
    session.distinct_values(session.source, "events", "event_type")
    view = view_for(session, "\\from events\n" + line, line, len(line))
    assert view is not None
    assert view.labels == ["purchase"]
    assert line[view.start:] == "'pur"


def test_quotes_keep_boolean_words_inside_the_value(sources):
    session = Session(sources)
    line = "\\where event_type = 'missing AND "
    assert view_for(session, "\\from events\n" + line, line, len(line)) is None


@pytest.mark.parametrize(("expression", "labels"), [
    ("sum(amount \\where ev", ["event_type"]),
    ("count(* \\where (amount > 1 AND (ev", ["event_type"]),
    ("sum(amount \\where event_type = 'pur", ["purchase"]),
    ("sum(amount \\where event_type = 'purchase' ", ["and", "or"]),
    ("sum(amount \\where event_type = 'purchase'), us", ["user_id"]),
])
def test_filtered_aggregate_uses_where_completion_until_its_closing_paren(expression, labels, sources):
    session = Session(sources)
    line = "\\select " + expression
    session.distinct_values(session.source, "events", "event_type")
    view = view_for(session, "\\from events\n" + line, line, len(line))
    assert view is not None
    assert view.labels == labels


@pytest.mark.parametrize(("marked", "expected", "label"), [
    (
        "\\where amount > 1 AND (ev|ent_stale = 'view')",
        "\\where amount > 1 AND (event_type = 'view')",
        "event_type",
    ),
    (
        "\\where event_type = 'pur|chase' OR amount > 1",
        '\\where event_type = "purchase" OR amount > 1',
        "purchase",
    ),
    (
        "\\select sum(am|ount) as total",
        "\\select sum(amount) as total",
        "amount",
    ),
    (
        "\\sel|ect amount",
        "\\select amount",
        "\\select",
    ),
    (
        "\\select sum(amount \\wh|ere event_type = 'purchase') as total",
        "\\select sum(amount \\where event_type = 'purchase') as total",
        "\\where",
    ),
    (
        "\\select sum(amount \\where amount > 1 AND (ev|ent_stale = 'purchase')) as total",
        "\\select sum(amount \\where amount > 1 AND (event_type = 'purchase')) as total",
        "event_type",
    ),
])
def test_tab_replaces_only_the_completion_token(marked, expected, label):
    async def scenario():
        app = D8RApp()
        async with app.run_test(size=(150, 58)) as pilot:
            column = marked.index("|")
            line = marked.replace("|", "")
            app.editor.load_text("\\from events\n" + line + "\n\\limit 5")
            app.editor.move_cursor((1, column))
            app.editor.focus()
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.palette.view is not None
            app.palette.highlighted = app.palette.view.labels.index(label)
            await pilot.press("tab")
            assert app.editor.text == "\\from events\n" + expected + "\n\\limit 5"
            assert app.focused is app.editor

    asyncio.run(scenario())


def test_tab_without_an_accept_preserves_document_selection_focus_and_dismissal():
    async def scenario():
        app = D8RApp()
        async with app.run_test(size=(150, 58)) as pilot:
            document = "\\from events\n\\where amount > 1 AND ev"
            app.editor.load_text(document)
            app.editor.move_cursor((1, len(document.splitlines()[1])))
            app.editor.focus()
            await pilot.pause()
            assert app.palette.is_open
            await pilot.press("escape", "tab", "e")
            assert app.editor.text == document + "e"
            assert app.focused is app.editor
            assert not app.palette.is_open

            await pilot.press("escape")
            assert app.palette.is_open
            await pilot.press("ctrl+a")
            selection = app.editor.selection
            document = app.editor.text
            await pilot.press("tab")
            assert app.editor.selection == selection
            assert app.editor.text == document
            assert app.focused is app.editor

            document = "\\from events\n\\select user_id"
            app.editor.load_text(document)
            app.editor.move_cursor((1, len(document.splitlines()[1])))
            await pilot.pause()
            assert not app.palette.is_open
            await pilot.press("tab")
            assert app.editor.text == document
            assert app.focused is app.editor

            app.editor.read_only = True
            await pilot.press("tab")
            assert app.editor.text == document
            assert app.focused is app.editor
            app.editor.read_only = False

            app.editor.move_cursor((1, len(document.splitlines()[1])))
            app.session.intellisense = False
            app.palette.sync()
            await pilot.press("tab")
            assert app.editor.text == document
            assert app.focused is app.editor
            assert not app.palette.is_open

    asyncio.run(scenario())


def test_pages_aliases_offer_workspace_actions_without_query_insertions(sources):
    session = Session(sources)
    for command in ("pages", "queries"):
        line = "\\" + command
        view = view_for(session, line, line, len(line))
        assert view is not None
        entry = next(entry for entry in view.entries if entry.label.lower() == command)
        assert entry.action == "toggle_pages"
        assert not entry.insert


def test_schema_qualified_table_acceptance_and_join_execute(sources, tmp_path):
    source = replace(sources["demo"], id="qualified", postgres={"schema": "etl"}, datasets={
        "etl.dataset_symbol": {
            **sources["demo"].datasets["events"],
            "foreign_keys": (ForeignKey(("user_id",), "etl.users", ("user_id",)),),
        },
        "etl.users": sources["demo"].datasets["users"],
        "public.dataset_symbol": sources["demo"].datasets["users"],
    })
    session = Session({source.id: source}, data_dir=tmp_path)

    async def scenario():
        app = D8RApp(session)
        async with app.run_test(size=(150, 58)) as pilot:
            app.editor.load_text('\\from "etl".data')
            app.editor.move_cursor((0, len(app.editor.text)))
            app.editor.focus()
            await pilot.pause()
            assert app.palette.view.labels == ["etl.dataset_symbol"]
            await pilot.press("tab")
            assert app.editor.text == "\\from etl.dataset_symbol d "
            app.editor.load_text(app.editor.text.rstrip() + "\n\\join us")
            app.editor.move_cursor((1, len("\\join us")))
            await pilot.pause()
            await pilot.press("tab")
            assert app.editor.text.endswith("\\join etl.users u ")
            await pilot.press("tab")
            assert app.editor.text.endswith("on d.user_id = u.user_id ")
            app.editor.load_text(app.editor.text.rstrip() + "\n\\select count(*) as n")
            await pilot.pause()
            await pilot.press("ctrl+enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            results = app.query_one("#results-table", DataTable)
            assert results.row_count == 1
            assert [str(cell) for cell in results.get_row_at(0)] == ["100"]

    asyncio.run(scenario())
    assert session.table_insert("\\from etl.dataset_symbol d", "etl.dataset_symbol") == "etl.dataset_symbol d2 "
    assert session.column_reference("\\from dataset_symbol s", "etl.dataset_symbol", "user_id") == "s.user_id"
    assert session.dataset_of("\\from dataset_symbol s", "s.user_id") == "etl.dataset_symbol"
    assert session.dataset_of("\\from dataset_symbol", "user_id") == "etl.dataset_symbol"


@pytest.mark.parametrize("prefix", ['etl.', '"etl".', '"etl"."data', '"et'])
def test_quoted_schema_prefixes_offer_the_same_physical_tables(prefix, sources, tmp_path):
    source = replace(sources["demo"], id="qualified", datasets={
        "public.dataset_symbol": sources["demo"].datasets["users"],
        "etl.dataset_symbol": sources["demo"].datasets["events"],
    })
    session = Session({source.id: source}, data_dir=tmp_path)
    for command in ("from", "open", "join", "union all", "intersect", "except"):
        line = "\\" + command + " " + prefix
        view = view_for(session, line, line, len(line))
        assert view is not None
        assert view.labels == ["etl.dataset_symbol"]


def test_cross_schema_bare_names_keep_selected_columns_and_execution(sources, tmp_path):
    source = replace(sources["demo"], id="qualified", postgres={"schema": "public"}, datasets={
        "etl.items": sources["demo"].datasets["events"],
        "public.items": sources["demo"].datasets["users"],
    })
    session = Session({source.id: source}, data_dir=tmp_path)
    for operand in ("items", '"items"', "public.items"):
        line = "\\group sc"
        document = "\\from " + operand + "\n" + line
        assert view_for(session, document, line, len(line)).labels == ["score"]
        result = session.run("\\from " + operand + "\n\\select count(*) as n")
        assert result.ok, result.error
        assert result.rows == [[25]]
    for operand in ("etl.items", '"etl".items'):
        line = "\\select " + operand + ".am"
        document = "\\from " + operand + "\n" + line
        view = view_for(session, document, line, len(line))
        assert view is not None
        assert view.labels == [operand + ".amount"]
        result = session.run("\\from " + operand + "\n\\select count(*) as n")
        assert result.ok, result.error
        assert result.rows == [[100]]
        line = "\\where " + operand + ".amount "
        view = view_for(session, "\\from " + operand + "\n" + line, line, len(line))
        assert view is not None and "=" in view.labels
        line = "\\select " + operand + ".amount_stale+1"
        column = line.index("amount_stale") + 2
        view = view_for(session, "\\from " + operand + "\n" + line, line, column)
        assert view is not None
        entry = next(entry for entry in view.entries if entry.label == operand + ".amount")
        assert line[:view.start] + entry.insert + line[view.end:] == "\\select " + operand + ".amount +1"

def test_tab_advances_from_command_to_table_and_closes_completed_fields():
    async def scenario():
        app = D8RApp()
        async with app.run_test(size=(120, 42)) as pilot:
            app.editor.load_text("")
            app.editor.focus()
            await pilot.press(*"\\from", "tab")
            assert app.editor.text == "\\from "
            assert app.palette.view.labels == ["events", "users"]
            await pilot.press("down", "tab")
            assert app.editor.text == "\\from users u "
            assert not app.palette.is_open
            assert app.focused is app.editor
            await pilot.press("enter", *"\\select", "tab", "u", "s", "tab")
            assert app.editor.text.endswith("\\select u.user_id ")
            assert not app.palette.is_open
            await pilot.press(",", "space")
            assert "u.region" in app.palette.view.labels
            # An already-present space goes directly to a table, not another command.
            app.editor.load_text("\\from ")
            app.editor.move_cursor((0, len(app.editor.text)))
            await pilot.press("tab")
            assert app.editor.text == "\\from events e "
            assert not app.palette.is_open

    asyncio.run(scenario())


def test_popup_fits_rendered_caret_after_wrapping_resizing_and_scrolling():
    async def scenario():
        app = D8RApp()
        async with app.run_test(size=(120, 42)) as pilot:
            editor = app.editor
            editor.load_text("\\from events\n\\select ")
            editor.move_cursor((1, len("\\select ")))
            editor.focus()
            await pilot.pause()

            def assert_clear_caret():
                assert app.palette.is_open
                assert editor.cursor_screen_offset not in app.palette.region
                assert app.palette.region in editor.content_region
                assert app.palette.region.height <= 8

            assert_clear_caret()
            # Long expressions soft-wrap: screen coordinates, not document rows,
            # determine whether the popup belongs above or below the caret.
            line = "\\select " + "user_id, " * 90
            editor.load_text("\\from events\n" + line)
            editor.move_cursor((1, len(line)))
            await pilot.pause()
            assert_clear_caret()
            assert app.palette.region.bottom <= editor.cursor_screen_offset.y
            await pilot.resize_terminal(80, 32)
            await pilot.pause()
            assert_clear_caret()
            # Mouse scrolling away from the caret must not leave a floating list.
            editor.scroll_home(animate=False)
            await pilot.pause()
            assert not app.palette.is_open
            editor.move_cursor((1, 8))
            await pilot.press("escape")
            assert app.palette.is_open
            app.query_one("#results-table", DataTable).focus()
            await pilot.pause()
            assert not app.palette.is_open

    asyncio.run(scenario())


def test_group_default_excludes_aggregate_dependencies_and_output_aliases(sources):
    session = Session(sources)
    document = (
        "\\from events e\n"
        "\\SELECT e.user_id as person, sum(e.amount) / 100 as total\n"
        "\\select e.event_type, e.user_id, count(* \\where e.path = '/') as n\n"
        "\\select year(e.timestamp) as year, upper(e.path) as path, 'x' as marker\n"
        "\\GROUP "
    )
    view = view_for(session, document, "\\GROUP ", len("\\GROUP "))
    assert view.entries[0].insert == "e.user_id, e.event_type "
    # Explicit choices are not replaced with inferred ones.
    document += "e.path"
    view = view_for(session, document, "\\gro", len("\\gro"))
    assert next(entry for entry in view.entries if entry.label == "\\group").insert == "\\group "


def test_group_completion_stays_in_its_cte_and_preserves_existing_groups():
    async def scenario():
        app = D8RApp()
        async with app.run_test(size=(140, 48)) as pilot:
            document = (
                "\\with totals\n"
                "  \\from events e\n"
                "  \\select e.event_type, sum(e.amount) as total\n"
                "  \\gro\n"
                "\\from totals t\n"
                "\\select t.event_type, t.total\n"
            )
            app.editor.load_text(document)
            app.editor.move_cursor((3, len("  \\gro")))
            app.editor.focus()
            await pilot.press("tab")
            expected = document.replace("  \\gro\n", "  \\group e.event_type \n")
            assert app.editor.text == expected
            assert not app.palette.is_open
            # Asking for the clause again navigates; it does not add outer fields.
            app.editor.move_cursor((2, len(app.editor.document[2])))
            await pilot.press(*"\\gro", "tab")
            assert app.editor.text == expected
            assert app.editor.cursor_location == (3, len("  \\group e.event_type "))
            await pilot.press("ctrl+enter")
            await app.workers.wait_for_complete()
            assert app.query_one("#results-table", DataTable).row_count == 4
            assert app.session.history[0].doc == expected
            app.editor.load_text("\\from events\n\\select event_type")
            app.editor.move_cursor((1, len("\\select event_type")))
            await pilot.press(*"\\gro", "tab")
            assert app.editor.text == "\\from events\n\\select event_type\n\\group event_type "

    asyncio.run(scenario())
