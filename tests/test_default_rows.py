"""Default query rows: explicit-limit precedence, durable settings and real UI edits."""

from __future__ import annotations

import asyncio
import json

import pytest
from textual.widgets import Button, DataTable, Input, OptionList, Static, TextArea

from d8r.ai.client import AIConfig
from d8r.ai.context import AIContext
from d8r.tui.app import D8RApp
from d8r.tui.session import Session
from d8r.tui.settings import DefaultRowsScreen, SettingsScreen


DOCUMENT = "\\from events\n\\select user_id\n\\order user_id asc"


@pytest.mark.parametrize(("limit", "expected"), [(None, 50), (3, 3), (80, 80), (0, 0)])
def test_run_and_compile_agree_on_default_and_explicit_limits(sources, limit, expected):
    session = Session(sources)
    document = DOCUMENT + ("" if limit is None else f"\n\\limit {limit}")
    sql, message = session.compile(document)
    assert sql is not None, message
    assert session.history == []
    compiled_rows = [list(row) for row in session.source.con.raw_sql(sql).fetchall()]
    result = session.run(document)
    assert result.ok, result.error
    assert result.total == expected
    assert result.rows == compiled_rows
    assert session.history[0].doc == document


def test_disabling_default_restores_unlimited_execution_after_restart(sources):
    original = Session(sources)
    original.update_settings(default_rows=0)
    restored = Session(sources)
    result = restored.run(DOCUMENT)
    assert result.ok, result.error
    assert result.total == 100
    sql, message = restored.compile(DOCUMENT)
    assert sql is not None, message
    assert len(restored.source.con.raw_sql(sql).fetchall()) == 100


def test_default_never_truncates_temp_materialization_or_its_compile():
    session = Session()
    document = DOCUMENT + "\n\\temp kept"
    sql, message = session.compile(document)
    assert sql is not None, message
    assert "kept" not in session.source.datasets
    assert len(session.source.con.raw_sql(sql).fetchall()) == 100
    result = session.run(document)
    assert result.ok, result.error
    assert session.run("\\from kept\n\\select count(user_id) as n").rows == [[100]]
    # Reading the materialized table normally is still an ordinary capped Run.
    assert session.run("\\from kept").total == 50
    explicit = session.run(DOCUMENT + "\n\\limit 3\n\\temp few")
    assert explicit.ok, explicit.error
    assert session.run("\\from few\n\\select count(user_id) as n").rows == [[3]]


def test_ai_samples_keep_their_own_limit(sources):
    session = Session(sources)
    session.update_settings(default_rows=1, ai=AIConfig(sample_rows=80))
    context = AIContext(session, session.active_id, DOCUMENT)
    sample = json.loads(asyncio.run(context.call_tool("sample_rows", {"table": "events"})))
    assert "error" not in sample
    assert len(sample["rows"]) == 80
    assert session.history == []
    result = session.run(DOCUMENT)
    assert result.ok, result.error
    assert result.total == 1


@pytest.mark.parametrize("invalid", [-1, 1_000_001, True, 1.5, "25"])
def test_invalid_settings_preserve_live_and_saved_limit(sources, invalid):
    session = Session(sources)
    session.update_settings(default_rows=12)
    before = session.settings_path.read_bytes()
    with pytest.raises(ValueError):
        session.update_settings(default_rows=invalid)
    assert session.default_rows == 12
    assert session.settings_path.read_bytes() == before
    assert Session(sources).run(DOCUMENT).total == 12


def test_maximum_default_is_valid_and_old_settings_keep_default_without_rewrite(sources, tmp_path):
    path = tmp_path / "settings.json"
    original = b'{"intellisense":false}\n'
    path.write_bytes(original)
    session = Session(sources, data_dir=tmp_path)
    assert session.default_rows == 50
    assert path.read_bytes() == original
    session.update_settings(default_rows=1_000_000)
    restored = Session(sources, data_dir=tmp_path)
    assert restored.default_rows == 1_000_000
    assert restored.run(DOCUMENT).total == 100


async def open_rows(app, pilot):
    await pilot.press("ctrl+comma")
    await pilot.pause()
    settings = app.screen
    assert isinstance(settings, SettingsScreen)
    menu = settings.query_one("#settings-menu", OptionList)
    menu.highlighted = next(index for index, row in enumerate(settings.rows)
                            if row.label == "Default rows returned")
    menu.focus()
    await pilot.press("enter")
    await pilot.pause()
    assert isinstance(app.screen, DefaultRowsScreen)
    assert app.screen.query_one("#default-rows", Input).has_focus
    return settings, menu


async def enter_rows(app, pilot, value):
    app.screen.query_one("#default-rows", Input).focus()
    await pilot.press("home", "shift+end", "backspace", *str(value))


async def press_form_button(app, pilot, selector):
    button = app.screen.query_one(selector, Button)
    button.focus()
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()


def test_general_rows_form_saves_cancels_and_restores_at_80_by_24():
    async def scenario():
        app = D8RApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.editor.load_text(DOCUMENT)
            settings, menu = await open_rows(app, pilot)
            selected = menu.highlighted
            assert app.screen.query_one("#default-rows", Input).value == "50"
            await enter_rows(app, pilot, 12)
            await press_form_button(app, pilot, "#rows-settings-cancel")
            assert app.screen is settings and menu.has_focus
            assert menu.highlighted == selected
            assert app.session.default_rows == 50
            assert not app.session.settings_path.exists()

            await pilot.press("enter")
            await pilot.pause()
            await enter_rows(app, pilot, 12)
            await press_form_button(app, pilot, "#rows-settings-save")
            assert app.screen is settings and menu.has_focus
            assert menu.highlighted == selected
            assert Session().default_rows == 12
            await pilot.press("escape", "escape")
            await pilot.press("ctrl+enter")
            await app.workers.wait_for_complete()
            assert app.query_one("#results-table", DataTable).row_count == 12
            assert app.editor.text == DOCUMENT
            app.action_compile()
            await app.workers.wait_for_complete()
            sql = app.query_one("#sql-text", TextArea).text
            with app.session.source.con.raw_sql(sql) as cursor:
                assert len(cursor.fetchall()) == 12
            assert app.editor.text == DOCUMENT

            settings, menu = await open_rows(app, pilot)
            await enter_rows(app, pilot, 0)
            await press_form_button(app, pilot, "#rows-settings-save")
            assert app.screen is settings and menu.has_focus
            assert Session().run(DOCUMENT).total == 100

    asyncio.run(scenario())


def test_invalid_and_stale_form_saves_keep_draft_and_previous_setting():
    async def scenario():
        app = D8RApp()
        app.session.update_settings(default_rows=12)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            settings, menu = await open_rows(app, pilot)
            form = app.screen
            before = app.session.settings_path.read_bytes()
            for invalid in ("", "-1", "1000001"):
                await enter_rows(app, pilot, invalid)
                await press_form_button(app, pilot, "#rows-settings-save")
                assert app.screen is form
                assert form.query_one("#default-rows", Input).value == invalid
                assert form.query_one("#default-rows", Input).has_focus
                assert str(form.query_one("#rows-settings-error", Static).content)
                assert app.session.default_rows == 12
                assert app.session.settings_path.read_bytes() == before

            Session().update_settings(default_rows=80)
            saved = app.session.settings_path.read_bytes()
            await enter_rows(app, pilot, 25)
            await press_form_button(app, pilot, "#rows-settings-save")
            assert app.screen is form
            assert form.query_one("#default-rows", Input).value == "25"
            assert str(form.query_one("#rows-settings-error", Static).content)
            assert app.session.default_rows == 12
            assert app.session.settings_path.read_bytes() == saved
            assert Session().default_rows == 80
            await pilot.press("escape")
            assert app.screen is settings and menu.has_focus
            assert menu.highlighted == 1

    asyncio.run(scenario())
