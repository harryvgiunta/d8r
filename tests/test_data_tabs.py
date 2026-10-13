"""One `;`-separated document, many results: tabs, pins and the History seam.

The run keeps the first outcome in the Results pane and gives every later
query its own tab that expands out to the right of History. A pinned tab
survives the next run; an unpinned one belongs to the run that made it.
"""

from __future__ import annotations

import asyncio

from textual.widgets import Button, TabbedContent

from d8r.tui.app import D8RApp
from d8r.tui.results import ResultsTable

TWO_QUERIES = (
    "\\from events\n\\select user_id\n\\limit 2;\n\\from users\n\\select region\n\\limit 4"
)


def run_app(scenario):
    app = D8RApp()

    async def drive():
        async with app.run_test(size=(160, 50)) as pilot:
            await pilot.pause()
            await scenario(app, pilot)

    asyncio.run(drive())
    return app


def tabs_of(app: D8RApp) -> TabbedContent:
    return app.query_one("#result-tabs", TabbedContent)


def test_a_second_query_lands_on_its_own_tab_right_of_history():
    async def scenario(app, pilot):
        app._load_document(TWO_QUERIES)
        await pilot.pause()
        await pilot.press("ctrl+enter")
        await app.workers.wait_for_complete()
        for _ in range(6):
            await pilot.pause()

        # The first result stays on Results; the second becomes "Data 2"? no:
        # exactly one extra tab per extra query, numbered by creation order.
        assert app.query_one("#results-table", ResultsTable).row_count == 2
        assert list(app._data_tabs) == ["tab-data-1"]
        assert str(tabs_of(app).get_tab("tab-data-1").label) == "Data 1"
        assert app.query_one("#data-tab-data-1", ResultsTable).row_count == 4
        # Tabs expand outward: Results · SQL · History · Data 1.
        order = [tab.id.removeprefix("--content-tab-") for tab in tabs_of(app).query("ContentTab")]
        assert order == ["tab-results", "tab-sql", "tab-history", "tab-data-1"]
        # The fresh result comes forward.
        assert tabs_of(app).active == "tab-data-1"
        # The footer names the multi-result run.
        assert "2 results" in str(app.query_one("#status").content)

    run_app(scenario)


def test_unpinned_tabs_die_with_their_run_and_pinned_ones_stay():
    async def scenario(app, pilot):
        app._load_document(TWO_QUERIES)
        await pilot.pause()
        await pilot.press("ctrl+enter")
        await app.workers.wait_for_complete()
        for _ in range(6):
            await pilot.pause()

        # Pin the extra tab, then run a single-query document.
        pin = next(button for button in app.query(Button) if button.has_class("data-pin"))
        pin.press()
        for _ in range(4):
            await pilot.pause()
        assert app._data_tabs["tab-data-1"]["pinned"] is True
        assert str(tabs_of(app).get_tab("tab-data-1").label).startswith("◆")

        app._load_document("\\from events\n\\select amount\n\\limit 1")
        await pilot.press("ctrl+enter")
        await app.workers.wait_for_complete()
        for _ in range(6):
            await pilot.pause()

        # The pinned tab and its rows survive; nothing new was added by a
        # single-query run; the main pane shows the new result.
        assert list(app._data_tabs) == ["tab-data-1"]
        assert app.query_one("#data-tab-data-1", ResultsTable).row_count == 4
        assert app.query_one("#results-table", ResultsTable).row_count == 1
        assert tabs_of(app).active == "tab-results"

    run_app(scenario)


def test_pinning_the_main_result_keeps_it_through_the_next_run():
    async def scenario(app, pilot):
        app._load_document("\\from users\n\\select region\n\\limit 3")
        await pilot.pause()
        await pilot.press("ctrl+enter")
        await app.workers.wait_for_complete()
        for _ in range(6):
            await pilot.pause()

        app.query_one("#results-pin", Button).press()
        for _ in range(4):
            await pilot.pause()
        assert list(app._data_tabs) == ["tab-data-1"]
        assert app.query_one("#data-tab-data-1", ResultsTable).row_count == 3
        assert tabs_of(app).active == "tab-data-1"

        app._load_document("\\from events\n\\select user_id\n\\limit 1")
        await pilot.press("ctrl+enter")
        await app.workers.wait_for_complete()
        for _ in range(6):
            await pilot.pause()
        assert app.query_one("#data-tab-data-1", ResultsTable).row_count == 3
        assert app.query_one("#results-table", ResultsTable).row_count == 1

    run_app(scenario)


def test_closing_a_data_tab_returns_to_results():
    async def scenario(app, pilot):
        app._load_document(TWO_QUERIES)
        await pilot.pause()
        await pilot.press("ctrl+enter")
        await app.workers.wait_for_complete()
        for _ in range(6):
            await pilot.pause()

        closer = next(button for button in app.query(Button) if button.has_class("data-close"))
        closer.press()
        for _ in range(4):
            await pilot.pause()
        assert app._data_tabs == {}
        assert tabs_of(app).active == "tab-results"

    run_app(scenario)
