"""Whole-query history hints through the real editor palette."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import Mock

import pytest
from textual.widgets import TextArea

from d8r.tui.app import D8RApp
from d8r.tui.palette import CommandPalette, view_for
from d8r.tui.session import HistoryEntry, Session
from d8r.storage import SettingsStore


def _run(doc: str, *, source: str = "demo", target: str = "demo") -> HistoryEntry:
    return HistoryEntry(at="2026-10-07 12:00:00+0000", source=source, target=target,
                        dialect="duckdb", rows=1, ms=1, doc=doc)


def _queries(view):
    return [] if view is None else [entry for entry in view.entries if entry.replace_document]


def test_history_counts_exact_documents_then_breaks_ties_by_latest_run(sources):
    session = Session(sources)
    frequent = "\\from events\n\\limit 10"
    recent = "\\from events\n\\limit 2"
    older = "\\from events\n\\limit 3"
    differently_spaced = "\\from  events\n\\limit 10"
    session.history[:] = [_run(doc) for doc in [recent, frequent, older, differently_spaced, frequent]]
    prefix = "\\from ev"
    view = view_for(session, prefix, prefix, len(prefix), row=0)
    offers = _queries(view)
    assert [entry.insert for entry in offers] == [frequent, recent, older, differently_spaced]
    assert "2 runs" in offers[0].label
    assert "limit 10" in offers[0].label
    assert "1 run" in offers[1].label
    assert any(entry.label == "events" and not entry.replace_document for entry in view.entries)
    assert view.entries[0].insert == frequent
    command_view = view_for(session, "\\", "\\", 1, row=0)
    assert not command_view.entries[0].replace_document


def test_history_bounds_display_without_truncating_the_accepted_document(sources):
    session = Session(sources)
    document = "\\from events\n\\where event_type = '" + "long literal " * 30 + "'\n\\limit 1"
    others = [f"\\from events\n\\limit {index}" for index in range(12)]
    session.history[:] = [_run(query) for query in [*others, document, document]]
    prefix = "\\from ev"
    view = view_for(session, prefix, prefix, len(prefix), row=0)
    offers = _queries(view)
    assert [entry.insert for entry in offers] == [document, *others[:9]]
    assert len(offers[0].label) < 128
    assert offers[0].label.endswith("…")
    assert any(entry.label == "events" and not entry.replace_document for entry in view.entries)


def test_history_uses_stable_target_and_legacy_active_source_fallback(sources, tmp_path):
    source = replace(sources["demo"], connection_key="saved-current")
    other = replace(source, id="other", connection_key="saved-other")
    SettingsStore(tmp_path).save({"source": "saved-current"})
    session = Session({"demo": source, "other": other}, data_dir=tmp_path)
    current = "\\from events\n\\limit 1"
    legacy = "\\from events\n\\limit 2"
    foreign = "\\from events\n\\limit 3"
    foreign_legacy = "\\from events\n\\limit 4"
    session.history[:] = [
        _run(current, source="previous-runtime-id", target="saved-current"),
        _run(legacy, target=""),
        _run(foreign, target="saved-other"),
        _run(foreign_legacy, source="other", target=""),
    ]
    prefix = "\\from ev"
    assert [entry.insert for entry in _queries(view_for(session, prefix, prefix, len(prefix)))] == [current, legacy]
    with session.target_source("other"):
        assert [entry.insert for entry in _queries(view_for(session, prefix, prefix, len(prefix)))] == [foreign, foreign_legacy]


@pytest.mark.parametrize(("literal", "expected"), [
    ("'A  ", "'A  B'"),
    ("'a", "'a  B'"),
    ("'A B", "'A B'"),
    ("'A''  ", "'A''  B'"),
])
def test_history_whitespace_matching_preserves_literal_case_and_spacing(literal, expected, sources):
    session = Session(sources)
    literals = ["'A  B'", "'a  B'", "'A B'", "'A''  B'"]
    session.history[:] = [_run("\\from events\n\\where event_type = " + value + "\n\\limit 1") for value in literals]
    prefix = " \t\\FROM   events\n  \\WHERE\tevent_type = " + literal
    line = prefix.split("\n")[-1]
    offers = _queries(view_for(session, prefix, line, len(line), row=1))
    assert [entry.insert for entry in offers] == ["\\from events\n\\where event_type = " + expected + "\n\\limit 1"]


@pytest.mark.parametrize("accept", ["enter", "tab", "click"])
def test_history_accepts_exact_multiline_document_without_execution_and_is_undoable(accept, monkeypatch):
    async def scenario():
        app = D8RApp()
        document = "  \\from events\n\\select event_type\n\\limit 3\n"
        app.session.history[:] = [_run(document)]
        execute = Mock(side_effect=AssertionError("history acceptance must not execute"))
        monkeypatch.setattr(app.session, "run", execute)
        async with app.run_test(size=(150, 58)) as pilot:
            prefix = "\\from events\n\\select event_type\n\\limit "
            app.editor.load_text(prefix)
            app.editor.move_cursor((2, len("\\limit ")))
            app.editor.focus()
            await pilot.pause()
            assert app.palette.is_open
            entry = next(entry for entry in _queries(app.palette.view) if entry.insert == document)
            app.palette.highlighted = app.palette.view.entries.index(entry)
            if accept == "click":
                await pilot.click("#palette", offset=(3, 1))
            else:
                await pilot.press(accept)
            await pilot.pause()
            assert app.editor.text == document
            assert app.editor.cursor_location == (3, 0)
            assert app.focused is app.editor
            execute.assert_not_called()
            await pilot.press("ctrl+z")
            assert app.editor.text == prefix
            execute.assert_not_called()

    asyncio.run(scenario())


def test_mid_document_history_never_replaces_existing_suffix_but_normal_completion_works():
    async def scenario():
        app = D8RApp()
        app.session.history[:] = [_run("\\from events\n\\select amount\n\\limit 1")]
        async with app.run_test(size=(150, 58)) as pilot:
            document = "\\from events\n\\select am_stale + 1\n\\limit 9"
            app.editor.load_text(document)
            app.editor.move_cursor((1, len("\\select am")))
            app.editor.focus()
            await pilot.pause()
            assert not _queries(app.palette.view)
            app.palette.highlighted = app.palette.view.labels.index("amount")
            await pilot.press("tab")
            assert app.editor.text == "\\from events\n\\select amount + 1\n\\limit 9"

    asyncio.run(scenario())


def test_history_respects_dismissal_disabled_intellisense_and_function_body_palettes():
    async def scenario():
        app = D8RApp()
        app.session.history[:] = [_run("\\from events\n\\limit 3")]
        async with app.run_test(size=(150, 58)) as pilot:
            prefix = "\\from events\n\\limit "
            app.editor.load_text(prefix)
            app.editor.move_cursor((1, len("\\limit ")))
            app.editor.focus()
            await pilot.pause()
            assert _queries(app.palette.view)
            await pilot.press("escape", "tab", "3")
            assert app.editor.text == prefix + "3"
            assert not app.palette.is_open
            await pilot.press("backspace", "escape")
            assert _queries(app.palette.view)
            app.session.intellisense = False
            app.palette.sync()
            await pilot.press("tab")
            assert not app.palette.is_open
            assert app.editor.text == prefix
            app.session.intellisense = True
            app.action_fn()
            await pilot.pause()
            body = app.screen.query_one("#fn-body", TextArea)
            palette = app.screen.query_one("#fn-palette", CommandPalette)
            body.load_text("\\from ev")
            body.move_cursor((0, len(body.text)))
            body.focus()
            await pilot.pause()
            assert palette.view is not None
            assert "events" in palette.view.labels
            assert not _queries(palette.view)

    asyncio.run(scenario())
