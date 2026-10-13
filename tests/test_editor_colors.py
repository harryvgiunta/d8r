"""Rendered color regressions: stale theme backgrounds and lexical boundaries."""

import asyncio

from textual.widgets import OptionList, TextArea

from d8r.tui.app import D8RApp
from d8r.tui.session import Session


def _token_style(strip, token):
    offset = strip.text.index(token)
    for segment in strip:
        if offset < len(segment.text):
            return segment.style
        offset -= len(segment.text)
    raise AssertionError(f"Token {token!r} was not rendered")


def test_agent_status_backgrounds_follow_live_theme(sources, tmp_path, monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)

    async def scenario():
        app = D8RApp(Session(sources, data_dir=tmp_path))
        async with app.run_test(size=(150, 45)) as pilot:
            chat = app.ai_chats.create(app._ai_target())
            chat.unread = True
            app.ai_chats.changed(chat)
            app.action_agents()
            await pilot.pause()
            foregrounds = []
            for theme in ("cozy-pastel", "nord", "rose-pine-dawn"):
                assert app.update_settings(theme=theme)
                await pilot.pause()
                listing = app.query_one("#agents-list", OptionList)
                listing.highlighted = None
                app.editor.focus()
                await pilot.pause()
                for selector in ("#agents-list", "#agents-summary"):
                    widget = app.query_one(selector)
                    strips = widget.render_lines(widget.size.region)
                    ready = next(strip for strip in strips if "Ready" in strip.text)
                    style = _token_style(ready, "Ready")
                    assert style.bgcolor == widget.rich_style.bgcolor
                    if selector == "#agents-list":
                        foregrounds.append(style.color)
                        unread = next(strip for strip in strips if "unread" in strip.text)
                        assert _token_style(unread, "unread").bgcolor == widget.rich_style.bgcolor
                assert chat.unread
            assert len(set(foregrounds)) == 3

    asyncio.run(scenario())


def test_query_colors_preserve_quoted_tokens_and_identifier_boundaries(sources, tmp_path):
    async def scenario():
        app = D8RApp(Session(sources, data_dir=tmp_path))
        async with app.run_test(size=(160, 45)) as pilot:
            editor = app.editor
            text = '\\select concat(\'café \\where 42 it\'\'s\', "and 7", col42, e.order, @minimum, -2.5e+3) as total'
            editor.load_text(text)
            await pilot.pause()
            line = editor.get_line(0)

            def style(token):
                return line.get_style_at_offset(app.console, text.index(token))

            assert style("\\select").color != style("concat").color
            assert style("concat").color != style("café").color
            assert style("café") == style("\\where") == style("42 it") == style("and 7")
            assert style("col42").color is None
            assert style("e.order").color is None
            assert style("order").color is None
            assert style("@minimum").color is not None
            assert style("2.5e+3").color != style("café").color
            assert editor.text == text
            editor.load_text("\\where name = 'unfinished \\select 123")
            await pilot.pause()
            line = editor.get_line(0)
            assert line.get_style_at_offset(app.console, 15) == line.get_style_at_offset(app.console, 34)

    asyncio.run(scenario())


def test_editor_rendering_recolors_after_theme_switch_and_edit(sources, tmp_path, monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)

    async def scenario():
        app = D8RApp(Session(sources, data_dir=tmp_path))
        async with app.run_test(size=(160, 45)) as pilot:
            text = "\\select sum(amount) / 100 as total"
            app.editor.load_text(text)
            app.query_one("#schema-tree").focus()
            await pilot.pause()
            before = app.editor.render_line(0)
            assert _token_style(before, "sum").color != _token_style(before, "amount").color
            assert _token_style(before, "100").color != _token_style(before, "amount").color
            assert app.update_settings(theme="cozy-pastel")
            await pilot.pause()
            after = app.editor.render_line(0)
            assert _token_style(after, "sum").color != _token_style(before, "sum").color
            assert app.editor.text == text
            app.action_fn()
            await pilot.pause()
            body = app.query_one("#fn-body", TextArea)
            body.load_text("\\select 'text' as label")
            body.move_cursor((0, len(body.text)))
            body.focus()
            await pilot.press("x")
            await pilot.pause()
            assert body.text.endswith("labelx")
            await pilot.press("ctrl+z")
            await pilot.pause()
            assert body.text == "\\select 'text' as label"
            line = body.get_line(0)
            assert line.get_style_at_offset(app.console, 1).color != line.get_style_at_offset(app.console, 10).color

    asyncio.run(scenario())
