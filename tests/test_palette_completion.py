"""Predicate completion and accept-only Tab regressions."""

from __future__ import annotations

import asyncio

import pytest

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
            assert app.palette.is_open
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
