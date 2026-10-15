"""Whole-query history hints through the real editor palette."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from unittest.mock import Mock

import pytest
from textual.widgets import TextArea

from d8r.tui.app import D8RApp, HistoryPreview
from d8r.tui.palette import CommandPalette, view_for
from d8r.tui.session import HistoryEntry, Session
from d8r.storage import SettingsStore, WorkspaceStore


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
    assert view.entries[0].label == "events"
    first_history = view.entries.index(offers[0])
    assert all(entry.replace_document for entry in view.entries[first_history:])
    assert not _queries(view_for(session, "\\", "\\", 1, row=0))


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
    current = "\\from events\n\\limit 1"
    legacy = "\\from events\n\\limit 2"
    foreign = "\\from events\n\\limit 3"
    foreign_legacy = "\\from events\n\\limit 4"
    entries = [
        _run(current, source="previous-runtime-id", target="saved-current"),
        _run(legacy, target=""),
        _run(foreign, target="saved-other"),
        _run(foreign_legacy, source="other", target=""),
    ]
    store = WorkspaceStore(tmp_path)
    store.save({**store.document, "history": [asdict(entry) for entry in entries]})
    session = Session({"demo": source, "other": other}, data_dir=tmp_path)
    prefix = "\\from ev"
    assert [entry.insert for entry in _queries(view_for(session, prefix, prefix, len(prefix)))] == [current, legacy]
    with session.target_source("other"):
        assert [entry.insert for entry in _queries(view_for(session, prefix, prefix, len(prefix)))] == [foreign, foreign_legacy]


@pytest.mark.parametrize("prefix", [
    "\\from events",
    "\\from events ",
    "\\from events e",
    "\\from events\n\\select am",
    "\\from events \\where am",
    "\\with recent\n  \\from ev",
    "\\from events\n\\join us",
    "\\from events\n\\limit ",
    "\\from events\n",
    "\\fro",
])
def test_history_is_absent_after_source_choice_or_in_later_clauses(prefix, sources):
    session = Session(sources)
    session.history[:] = [_run(prefix + "ount\n\\limit 1")]
    line = prefix.rsplit("\n", 1)[-1]
    assert not _queries(view_for(session, prefix, line, len(line), row=prefix.count("\n")))


def test_history_opening_source_matching_preserves_quoted_identifiers(sources):
    session = Session(sources)
    queries = ['\\OPEN "A  B"\n\\limit 1', '\\open "a  B"\n\\limit 2']
    session.history[:] = [_run(query) for query in queries]
    prefix = ' \t\\open   "A  '
    assert [entry.insert for entry in _queries(view_for(session, prefix, prefix, len(prefix)))] == queries[:1]


@pytest.mark.parametrize("accept", ["enter", "tab", "click"])
def test_history_accepts_exact_multiline_document_without_execution_and_is_undoable(accept, monkeypatch):
    async def scenario():
        app = D8RApp()
        document = "  \\from events\n\\select event_type\n\\limit 3\n"
        app.session.history[:] = [_run(document)]
        execute = Mock(side_effect=AssertionError("history acceptance must not execute"))
        monkeypatch.setattr(app.session, "run", execute)
        async with app.run_test(size=(150, 58)) as pilot:
            prefix = "\\from ev"
            app.editor.load_text(prefix)
            app.editor.move_cursor((0, len(prefix)))
            app.editor.focus()
            await pilot.pause()
            assert app.palette.is_open
            entry = next(entry for entry in _queries(app.palette.view) if entry.insert == document)
            app.palette.highlighted = app.palette.view.entries.index(entry)
            if accept == "click":
                await pilot.click("#palette", offset=(3, 2))
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


def test_history_escape_keeps_contextual_completion_and_respects_disabled_and_function_palettes():
    async def scenario():
        app = D8RApp()
        app.session.history[:] = [_run("\\from events\n\\limit 3")]
        async with app.run_test(size=(150, 58)) as pilot:
            prefix = "\\from e"
            app.editor.load_text(prefix)
            app.editor.move_cursor((0, len(prefix)))
            app.editor.focus()
            await pilot.pause()
            assert _queries(app.palette.view)
            caret = app.editor.cursor_location
            await pilot.press("escape")
            assert app.editor.text == prefix
            assert app.editor.cursor_location == caret
            assert not _queries(app.palette.view)
            assert "events" in app.palette.view.labels
            await pilot.press("v")
            assert not _queries(app.palette.view)
            assert "events" in app.palette.view.labels
            await pilot.press("escape")
            assert not app.palette.is_open
            await pilot.press("escape")
            assert "events" in app.palette.view.labels
            assert not _queries(app.palette.view)
            app.session.intellisense = False
            app.palette.sync()
            await pilot.press("tab")
            assert not app.palette.is_open
            assert app.editor.text == prefix + "v"
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


def test_history_only_escape_leaves_intellisense_enabled_for_the_next_clause():
    async def scenario():
        app = D8RApp()
        app.session.history[:] = [_run("\\from archived_table\n\\limit 3")]
        async with app.run_test(size=(120, 40)) as pilot:
            prefix = "\\from arch"
            app.editor.load_text(prefix)
            app.editor.move_cursor((0, len(prefix)))
            app.editor.focus()
            await pilot.pause()
            assert all(entry.replace_document for entry in app.palette.view.entries)
            await pilot.press("escape")
            assert not app.palette.is_open
            await pilot.press("i")
            assert app.editor.text == prefix + "i"
            assert not app.palette.is_open
            await pilot.press("ctrl+a", "backspace")
            await pilot.pause()
            app.editor.load_text("\\from ev")
            app.editor.move_cursor((0, 8))
            await pilot.pause()
            assert "events" in app.palette.view.labels
            await pilot.press("tab", "space")
            assert not _queries(app.palette.view)
            assert app.editor.text == "\\from events e  "

    asyncio.run(scenario())


def test_history_preview_follows_highlight_without_editing_and_fits_resizes(monkeypatch):
    async def scenario():
        app = D8RApp()
        documents = [
            "\\from events\n\\select event_type\n\\limit 3",
            "\\from events\n\\select amount\n\\where amount > 5\n\\limit 2",
        ]
        app.session.history[:] = [_run(doc) for doc in documents]
        execute = Mock(side_effect=AssertionError("preview must not execute"))
        monkeypatch.setattr(app.session, "run", execute)
        async with app.run_test(size=(150, 58)) as pilot:
            prefix = "\n" * 8 + "\\from ev"
            app.editor.load_text(prefix)
            app.editor.move_cursor((8, 8))
            app.editor.focus()
            await pilot.pause()
            preview = app.query_one("#history-preview", HistoryPreview)
            assert not preview.display
            await pilot.press("down")
            await pilot.pause()
            assert preview.display
            rendered = "\n".join(strip.text for strip in preview.render_lines(preview.size.region))
            assert "\\select event_type" in rendered
            assert "\\limit 3" in rendered
            await pilot.press("down")
            await pilot.pause()
            rendered = "\n".join(strip.text for strip in preview.render_lines(preview.size.region))
            assert "\\select amount" in rendered
            assert "\\where amount > 5" in rendered
            assert "event_type" not in rendered
            await pilot.click("#history-preview", offset=(2, 2))
            for size in [(80, 24), (120, 40)]:
                await pilot.resize_terminal(*size)
                # Splitter clamping and caret scrolling each schedule a layout frame.
                await pilot.pause(0.1)
                assert preview.display
                assert preview.region in app.query_one("#work-bench").content_region
                assert preview.region in app.screen.region
                assert not preview.region.overlaps(app.palette.region)
                assert app.editor.cursor_screen_offset not in preview.region
                assert app.focused is app.editor
                assert app.editor.text == prefix
                assert app.editor.cursor_location == (8, 8)
            await pilot.press("up", "up")
            await pilot.pause()
            assert not preview.display
            await pilot.press("down", "escape")
            await pilot.pause()
            assert not preview.display
            assert not _queries(app.palette.view)
            assert "events" in app.palette.view.labels
            assert app.editor.text == prefix
            execute.assert_not_called()

    asyncio.run(scenario())
