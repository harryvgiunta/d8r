"""Shared slots retain drafts and resize preferences across editor modes."""
from __future__ import annotations

import asyncio

from textual.widgets import Input, TabbedContent, TextArea

from d8r.tui.app import D8RApp
from d8r.tui.session import Session


def test_editor_modes_keep_geometry_drafts_and_one_live_assistant(tmp_path):
    async def scenario():
        app = D8RApp(Session(data_dir=tmp_path))
        async with app.run_test(size=(150, 50)) as pilot:
            document = app.editor.text
            editor = app.editor
            panel = app.ai_panel
            app.action_ai()
            panel.query_one("#ai-input", TextArea).load_text("Query conversation draft")
            app.query_one("#explorer-splitter").focus()
            await pilot.press("shift+right")
            app.query_one("#editor-splitter").focus()
            await pilot.press("down", "down")
            await pilot.pause()
            selectors = ("#explorer-slot", "#editor-slot", "#workspace-ai")
            sizes = [app.query_one(selector).size for selector in selectors]
            app.action_fn()
            await pilot.pause()
            assert app.query_one("#fn-list-pane").display
            assert not app.query_one("#schema-pane").display
            assert app.function_editor.display and panel.display
            assert [app.query_one(selector).size for selector in selectors] == sizes
            assert app.ai_panel is panel and app.editor is editor
            assert len(app.screen_stack) == 1
            form = app.function_editor
            form.query_one("#fn-name", Input).value = "draft_only"
            form.query_one("#fn-body", TextArea).load_text("\\from users\n\\limit 2")
            panel.query_one("#ai-input", TextArea).load_text("Function conversation draft")
            app.action_workspace()
            await pilot.pause()
            assert app.editor.text == document
            assert panel.query_one("#ai-input", TextArea).text == "Query conversation draft"
            assert [app.query_one(selector).size for selector in selectors] == sizes
            app.action_fn()
            await pilot.pause()
            assert form.query_one("#fn-name", Input).value == "draft_only"
            assert form.query_one("#fn-body", TextArea).text == "\\from users\n\\limit 2"
            assert panel.query_one("#ai-input", TextArea).text == "Function conversation draft"
            assert not app.session.fns and not app.session.history
            app.exit()
        restored = D8RApp(Session(data_dir=tmp_path))
        async with restored.run_test(size=(150, 50)) as pilot:
            await pilot.pause()
            assert restored.function_mode
            assert [restored.query_one(selector).size for selector in selectors] == sizes
            assert restored.function_editor.query_one("#fn-body", TextArea).text == "\\from users\n\\limit 2"
            assert restored.editor.text == document
            assert not restored.session.fns

    asyncio.run(scenario())


def test_drag_commits_once_escape_cancels_and_viewport_does_not_save(tmp_path):
    async def scenario():
        app = D8RApp(Session(data_dir=tmp_path))
        async with app.run_test(size=(150, 50)) as pilot:
            app.action_ai()
            await pilot.pause()
            handle = app.query_one("#explorer-splitter")
            origin = handle.region.offset
            await pilot.mouse_down(handle)
            await pilot.hover(offset=(origin.x + 7, origin.y))
            assert app.query_one("#explorer-slot").size.width == 43
            assert app.session.pane_sizes["explorer"] == 36
            await pilot.mouse_up(offset=(origin.x + 7, origin.y))
            await pilot.pause()
            assert app.session.pane_sizes["explorer"] == 43
            before = app.session.settings_path.read_bytes()
            origin = handle.region.offset
            await pilot.mouse_down(handle)
            await pilot.hover(offset=(origin.x - 4, origin.y))
            await pilot.press("escape")
            assert app.query_one("#explorer-slot").size.width == 43
            assert app.session.settings_path.read_bytes() == before
            await pilot.resize_terminal(80, 30)
            await pilot.pause()
            assert app.query_one("#explorer-slot").size.width < 43
            assert app.query_one("#work-bench").size.width >= 30
            await pilot.mouse_down(handle)
            await pilot.mouse_up(handle)
            assert app.session.settings_path.read_bytes() == before
            await pilot.resize_terminal(150, 50)
            await pilot.pause()
            assert app.query_one("#explorer-slot").size.width == 43
            assert app.query_one("#workspace-ai").outer_size.width == 44
            assert app.session.settings_path.read_bytes() == before

    asyncio.run(scenario())


def test_unused_function_form_does_not_restore_an_old_source(tmp_path):
    async def scenario():
        app = D8RApp(Session(data_dir=tmp_path))
        async with app.run_test(size=(140, 45)) as pilot:
            app.select_source("mysql")
            await pilot.pause()
            app.exit()
        restored = D8RApp(Session(data_dir=tmp_path))
        async with restored.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            assert not restored.session.workspace.get("function_draft")
            restored.action_fn()
            await pilot.pause()
            assert restored.function_editor.source_id == "mysql"

    asyncio.run(scenario())


def test_hidden_agents_and_assistant_survive_stale_tab_restore_and_mode_changes(tmp_path):
    async def scenario():
        app = D8RApp(Session(data_dir=tmp_path))
        async with app.run_test(size=(150, 50)) as pilot:
            app.action_ai()
            app.ai_panel.query_one("#ai-input", TextArea).load_text("Unsent query draft")
            app.action_fn()
            await pilot.press("ctrl+j")
            await pilot.pause()
            tabs = app.query_one("#explorer-tabs", TabbedContent)
            assert app.function_mode and tabs.active == "tab-agents"
            app.toggle_pane("agents")
            app.toggle_pane("ai")
            await pilot.pause()
            assert tabs.active != "tab-agents"
            assert not tabs.get_tab("tab-agents").display
            app.action_workspace()
            await pilot.pause()
            assert not app.ai_panel.display
            assert app.ai_panel.query_one("#ai-input", TextArea).text == "Unsent query draft"
            app.action_fn()
            app.exit()

        # Settings may commit just before the tab's workspace autosave. A hidden
        # remembered tab must not leave the restored explorer blank.
        session = Session(data_dir=tmp_path)
        session.save_workspace(explorer_tab="agents")
        restored = D8RApp(session)
        async with restored.run_test(size=(150, 50)) as pilot:
            await pilot.pause()
            tabs = restored.query_one("#explorer-tabs", TabbedContent)
            assert restored.function_mode
            assert tabs.active == "tab-schema"
            assert not tabs.get_tab("tab-agents").display
            assert not restored.ai_panel.display
            assert not restored.query_one("#ai-splitter").display
            assert restored.query_one("#fn-list-pane").visible
            restored.action_workspace()
            await pilot.pause()
            assert not restored.ai_panel.display
            assert restored.ai_panel.query_one("#ai-input", TextArea).text == "Unsent query draft"
            await pilot.press("ctrl+j")
            await pilot.pause()
            assert tabs.active == "tab-agents" and tabs.get_tab("tab-agents").display

    asyncio.run(scenario())
