"""Value-cache preferences: atomic persistence and real settings menu edits."""

from __future__ import annotations

import asyncio

import pytest
from textual.widgets import Button, Input, OptionList, Static

from d8r.tui.app import D8RApp
from d8r.tui.session import Session
from d8r.tui.settings import SettingsScreen, ValueCacheLimitScreen


def test_old_settings_load_cache_defaults_without_rewriting(sources, tmp_path):
    path = tmp_path / "settings.json"
    original = b'{"intellisense":false}\n'
    path.write_bytes(original)
    session = Session(sources, data_dir=tmp_path)
    assert not session.memory_error
    assert session.value_cache_enabled is True
    assert session.value_cache_limit == 1000
    assert session.cached_value_columns == 0
    assert path.read_bytes() == original

    session.update_settings(value_cache_limit=10_000, value_cache_enabled=False)
    restored = Session(sources, data_dir=tmp_path)
    assert restored.value_cache_limit == 10_000
    assert restored.value_cache_enabled is False
    assert restored.intellisense is False
    session.update_settings(value_cache_limit=1)
    assert Session(sources, data_dir=tmp_path).value_cache_limit == 1


@pytest.mark.parametrize("changes", [
    {"value_cache_limit": 0, "value_cache_enabled": False},
    {"value_cache_limit": 10_001},
    {"value_cache_limit": True},
    {"value_cache_limit": 1.5},
    {"value_cache_limit": "3"},
    {"value_cache_enabled": 0, "value_cache_limit": 3},
    {"value_cache_enabled": "true"},
])
def test_invalid_preferences_preserve_file_live_settings_and_cache(sources, changes):
    session = Session(sources)
    session.update_settings(value_cache_limit=12)
    cached = session.distinct_values(session.source, "events", "user_id")
    before = session.settings_path.read_bytes()
    epoch = session.value_cache_epoch
    with pytest.raises(ValueError):
        session.update_settings(**changes)
    assert session.value_cache_enabled is True
    assert session.value_cache_limit == 12
    assert session.cached_values(session.source, "events", "user_id") == cached
    assert session.value_cache_epoch == epoch
    assert session.settings_path.read_bytes() == before
    restored = Session(sources)
    assert restored.value_cache_enabled is True
    assert restored.value_cache_limit == 12


async def open_cache_settings(app, pilot):
    await pilot.press("ctrl+comma")
    await pilot.pause()
    settings = app.screen
    assert isinstance(settings, SettingsScreen)
    sidebar = settings.query_one("#settings-sidebar", OptionList)
    sidebar.highlighted = sidebar.get_option_index("values")
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()
    menu = settings.query_one("#settings-menu", OptionList)
    assert menu.has_focus
    return settings, menu


async def select_row(settings, pilot, label):
    menu = settings.query_one("#settings-menu", OptionList)
    menu.highlighted = next(index for index, row in enumerate(settings.rows) if row.label == label)
    menu.focus()
    await pilot.press("enter")
    await pilot.pause()


async def enter_limit(app, pilot, value):
    field = app.screen.query_one("#value-cache-limit", Input)
    field.focus()
    await pilot.press("home", "shift+end", "backspace", *str(value))


async def press_form_button(app, pilot, selector):
    app.screen.query_one(selector, Button).focus()
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()


def test_limit_form_cancel_keeps_cache_and_save_changes_real_read_limit():
    async def scenario():
        app = D8RApp()
        source = app.session.source
        original = app.session.distinct_values(source, "events", "user_id")
        epoch = app.session.value_cache_epoch
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            settings, menu = await open_cache_settings(app, pilot)
            await select_row(settings, pilot, "Values per column")
            assert isinstance(app.screen, ValueCacheLimitScreen)
            assert app.screen.query_one("#value-cache-limit", Input).has_focus
            selected = menu.highlighted
            await enter_limit(app, pilot, 3)
            await press_form_button(app, pilot, "#value-cache-settings-cancel")
            assert app.screen is settings and menu.has_focus
            assert menu.highlighted == selected
            assert app.session.value_cache_limit == 1000
            assert app.session.value_cache_epoch == epoch
            assert app.session.cached_values(source, "events", "user_id") == original
            assert not app.session.settings_path.exists()

            await pilot.press("enter")
            await pilot.pause()
            await enter_limit(app, pilot, 3)
            await press_form_button(app, pilot, "#value-cache-settings-save")
            assert app.screen is settings and menu.has_focus
            assert menu.highlighted == selected
            assert app.session.value_cache_limit == 3
            assert app.session.cached_values(source, "events", "user_id") is None
            values, capped = app.session.distinct_values(source, "events", "user_id")
            assert len(values) == 3 and capped
            restored = Session()
            assert restored.value_cache_limit == 3
            assert restored.cached_value_columns == 0

    asyncio.run(scenario())


def test_invalid_and_stale_limit_saves_keep_draft_and_previous_cache():
    async def scenario():
        app = D8RApp()
        app.session.update_settings(value_cache_limit=12)
        source = app.session.source
        cached = app.session.distinct_values(source, "events", "user_id")
        epoch = app.session.value_cache_epoch
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            settings, menu = await open_cache_settings(app, pilot)
            await select_row(settings, pilot, "Values per column")
            form = app.screen
            assert isinstance(form, ValueCacheLimitScreen)
            before = app.session.settings_path.read_bytes()
            for invalid in ("", "0", "10001"):
                await enter_limit(app, pilot, invalid)
                await press_form_button(app, pilot, "#value-cache-settings-save")
                assert app.screen is form
                assert form.query_one("#value-cache-limit", Input).value == invalid
                assert form.query_one("#value-cache-limit", Input).has_focus
                assert str(form.query_one("#value-cache-settings-error", Static).content)
                assert app.session.value_cache_limit == 12
                assert app.session.cached_values(source, "events", "user_id") == cached
                assert app.session.value_cache_epoch == epoch
                assert app.session.settings_path.read_bytes() == before

            Session().update_settings(value_cache_limit=80)
            saved = app.session.settings_path.read_bytes()
            await enter_limit(app, pilot, 25)
            await press_form_button(app, pilot, "#value-cache-settings-save")
            assert app.screen is form
            assert form.query_one("#value-cache-limit", Input).value == "25"
            assert str(form.query_one("#value-cache-settings-error", Static).content)
            assert app.session.value_cache_limit == 12
            assert app.session.cached_values(source, "events", "user_id") == cached
            assert app.session.value_cache_epoch == epoch
            assert app.session.settings_path.read_bytes() == saved
            assert Session().value_cache_limit == 80
            await pilot.press("escape")
            assert app.screen is settings and menu.has_focus

    asyncio.run(scenario())


def test_toggle_persists_and_clear_removes_real_cached_columns_without_saving():
    async def scenario():
        app = D8RApp()
        source = app.session.source
        app.session.distinct_values(source, "events", "user_id")
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            settings, _ = await open_cache_settings(app, pilot)
            await select_row(settings, pilot, "Cache column values")
            assert app.session.value_cache_enabled is False
            assert app.session.cached_values(source, "events", "user_id") is None
            assert Session().value_cache_enabled is False

            await select_row(settings, pilot, "Cache column values")
            assert app.session.value_cache_enabled is True
            assert Session().value_cache_enabled is True
            app.session.distinct_values(source, "events", "user_id")
            app.session.distinct_values(source, "users", "region")
            assert app.session.cached_value_columns == 2
            # Re-enter the category to read its current cache count.
            sidebar = settings.query_one("#settings-sidebar", OptionList)
            sidebar.highlighted = sidebar.get_option_index("general")
            await pilot.pause()
            sidebar.highlighted = sidebar.get_option_index("values")
            await pilot.pause()
            clear_row = next(row for row in settings.rows if row.label == "Clear cache")
            assert "2" in clear_row.detail
            before = app.session.settings_path.read_bytes()
            await select_row(settings, pilot, "Clear cache")
            assert app.session.cached_value_columns == 0
            assert app.session.cached_values(source, "events", "user_id") is None
            assert app.session.cached_values(source, "users", "region") is None
            assert app.session.settings_path.read_bytes() == before
            clear_row = next(row for row in settings.rows if row.label == "Clear cache")
            assert "0" in clear_row.detail

    asyncio.run(scenario())
