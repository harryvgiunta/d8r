"""The TUI end to end: the app, its palette, and its panes, driven by the pilot.

Every test here runs the real widgets against the real engine — the bundled
demo registry, the live schema seam, and, for the preview cap, a throwaway
SQLite snapshot. Nothing is mocked, and nothing here needs a network or a
terminal: `run_test` drives the app headlessly (no pytest-asyncio needed, the
suite stays on stdlib + pytest).
"""

from __future__ import annotations

import ast
import asyncio
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from textual.widgets import Button, DataTable, Input, OptionList, Select, Static, TabbedContent, TextArea

import anyql
from anyql.engine import DIALECTS, add_sqlite_source
from anyql.query import capabilities, schema_tables, set_schema_state
from anyql.tui.add_source import AddSourceModal
from anyql.tui.app import AnyqlApp
from anyql.tui.palette import VALUE_SUGGESTIONS, view_for
from anyql.tui.session import PREVIEW_ROW_CAP, VALUE_POOL_LIMIT, Session
from anyql.tui.settings import SettingsScreen
from anyql.tui.fn import FnScreen
from tests.conftest import REPO_ROOT

TUI_DIR = Path(anyql.__file__).resolve().parent / "tui"

# A document typed the way a person types one: `\` opens the palette, Enter
# either accepts a suggestion or means a newline, and nothing is swallowed.
TYPED_DOCUMENT = "\\from events\n\\select user_id\n\\limit 3"

# Pilot key names for the characters that are not their own key.
KEY_NAMES = {" ": "space", "\n": "enter"}


def run_app(scenario):
    """Mount a fresh app headlessly and hand it (and its pilot) to `scenario`."""
    app = AnyqlApp()

    async def drive():
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            await scenario(app, pilot)

    asyncio.run(drive())
    return app


async def type_document(pilot, text: str) -> None:
    """Type `text` one key at a time, the way a person would."""
    for character in text:
        await pilot.press(KEY_NAMES.get(character, character))


def text_of(app: AnyqlApp, selector: str) -> str:
    """A Static's text, without its styling."""
    return str(app.query_one(selector, Static).content)


def results_table(app: AnyqlApp) -> DataTable:
    return app.query_one("#results-table", DataTable)


def history_table(app: AnyqlApp) -> DataTable:
    return app.query_one("#history-table", DataTable)


@pytest.fixture(autouse=True)
def restore_schema_seam(_clean_schema_registry):
    """The schema registry is process-global: hand it back as it was found.

    It asks for the suite's reset first, so the seam this file found is the
    empty one, whatever installed a schema before it.
    """
    tables = list(schema_tables())
    caps = capabilities()
    yield
    set_schema_state(tables, caps)


@pytest.fixture
def snapshot(tmp_path: Path):
    """A local SQLite database, built the way a D1 snapshot arrives."""

    def build(name: str, rows: int, table: str = "readings") -> str:
        path = tmp_path / f"{name}.sqlite"
        connection = sqlite3.connect(path)
        connection.execute(f"create table {table} (id integer, name text)")
        connection.executemany(
            f"insert into {table} values (?, ?)",
            [(index, f"row-{index}") for index in range(rows)],
        )
        connection.commit()
        connection.close()
        return str(path)

    return build


# ---------------------------------------------------------------------------
# Booting
# ---------------------------------------------------------------------------


def test_app_boots_on_the_demo_source():
    """The explorer, the registry seam and the footer all reflect `demo`."""

    async def scenario(app, pilot):
        assert app.focused is app.editor
        datasets = app.query_one("#schema-tree").root.children
        assert [str(node.label) for node in datasets] == ["events  100 rows", "users  25 rows"]
        assert app.query_one("#source-select", Select).value == "demo"
        assert app.query_one("#dialect-select", Select).value == "duckdb"
        assert text_of(app, "#backend-pill") == "duckdb"
        assert "demo" in text_of(app, "#status")
        assert [str(column.label) for column in history_table(app).ordered_columns] == [
            "time",
            "source",
            "dialect",
            "rows",
            "ms",
            "document",
        ]
        # The seam the parser and the palette read is the active source's.
        assert [table.name for table in schema_tables()] == ["events", "users"]

    run_app(scenario)


def test_the_module_entry_point_boots_the_app():
    """`python -m anyql` really starts the IDE — proven, not assumed."""
    with Path(REPO_ROOT, "anyql", "__main__.py").open("rb") as handle:
        assert handle.read()  # the entry point exists where -m looks for it

    process = subprocess.Popen(
        [sys.executable, "-m", "anyql"],
        cwd=REPO_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        rendered = b""
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and b"ready \xe2\x80\xa2 demo" not in rendered:
            chunk = process.stdout.read1(4096)
            if not chunk:
                break
            rendered += chunk
    finally:
        process.terminate()
        process.wait(timeout=20)

    screen = rendered.decode("utf-8", "replace")
    assert "anyQL" in screen  # the header
    assert "Document" in screen  # the editor pane
    assert "ready · demo · duckdb" in screen  # the footer's status line


# ---------------------------------------------------------------------------
# Running, failing, compiling
# ---------------------------------------------------------------------------


def test_a_typed_document_fills_the_results_table():
    """Typing a document and running it renders rows, a status line, history."""

    async def scenario(app, pilot):
        app.editor.load_text("")
        await type_document(pilot, TYPED_DOCUMENT)
        assert app.editor.text == TYPED_DOCUMENT
        await pilot.press("ctrl+enter")
        await app.workers.wait_for_complete()

        table = results_table(app)
        assert table.row_count == 3
        assert [str(column.label) for column in table.ordered_columns] == ["user_id · int64"]
        assert [str(table.get_cell_at((row, 0))) for row in range(3)] == ["1", "8", "15"]
        assert app.query_one("#result-tabs").active == "tab-results"
        assert text_of(app, "#results-error") == ""
        assert text_of(app, "#results-status").startswith("3 rows · ")
        assert text_of(app, "#results-status").endswith("· demo · duckdb")
        assert history_table(app).row_count == 1
        assert str(history_table(app).get_cell_at((0, 1))) == "demo"

    run_app(scenario)


def test_pending_run_keeps_keys_responsive_and_owns_its_submission(monkeypatch):
    """A delayed real query cannot steal edits or share its connection mid-run."""
    from anyql.tui import session as session_module

    started = threading.Event()
    release = threading.Event()
    execute = session_module.execute
    calls = []

    def delayed_execute(*args, **kwargs):
        calls.append(threading.get_ident())
        started.set()
        if not release.wait(15):
            raise RuntimeError("query was not released while the UI was responsive")
        return execute(*args, **kwargs)

    monkeypatch.setattr(session_module, "execute", delayed_execute)

    async def scenario(app, pilot):
        app.editor.load_text(TYPED_DOCUMENT)
        await pilot.pause()
        try:
            await pilot.press("ctrl+enter")
            assert await asyncio.to_thread(started.wait, 2)
            assert app.run_busy
            assert len(calls) == 1
            assert calls[0] != threading.get_ident()

            # These are real key events, all handled before the query is released.
            app.editor.move_cursor((2, len("\\limit 3")))
            await pilot.press("backspace", "2", "ctrl+enter", "f5")
            edited = TYPED_DOCUMENT[:-1] + "2"
            assert app.editor.text == edited
            assert len(calls) == 1
            assert app.session.history == []
            assert "unavailable" in text_of(app, "#status")

            await pilot.press("ctrl+k")
            assert app.query_one("#sql-text", TextArea).text == ""
            app.query_one("#source-select", Select).value = "postgres"
            app.query_one("#dialect-select", Select).value = "mysql"
            await pilot.pause()
            assert app.session.active_id == "demo"
            assert app.session.dialect == "duckdb"
            assert app.query_one("#source-select", Select).value == "demo"
            assert app.query_one("#dialect-select", Select).value == "duckdb"

            # Completion must not issue a value query or parse changing temp schema.
            app.editor.load_text("\\from events\n\\where event_type = ")
            await pilot.pause()
            assert not app.palette.is_open
            app.editor.load_text(edited)
            await pilot.press("ctrl+comma")
            assert isinstance(app.screen, SettingsScreen)
            await pick(pilot, app, "Data source")
            await pick(pilot, app, "postgres")
            assert app.session.active_id == "demo"
            assert "unavailable" in text_of(app, "#status")
            await pilot.press("escape", "escape")
            assert not isinstance(app.screen, SettingsScreen)

            release.set()
            await app.workers.wait_for_complete()
            assert not app.run_busy and not app.session.busy
            assert app.editor.text == edited
            assert results_table(app).row_count == 3
            assert [str(results_table(app).get_cell_at((row, 0))) for row in range(3)] == ["1", "8", "15"]
            assert [(entry.doc, entry.source, entry.dialect) for entry in app.session.history] == [
                (TYPED_DOCUMENT, "demo", "duckdb")
            ]
            assert history_table(app).row_count == 1

            await pilot.press("ctrl+enter")
            await app.workers.wait_for_complete()
            assert results_table(app).row_count == 2
            assert [entry.doc for entry in app.session.history] == [edited, TYPED_DOCUMENT]
        finally:
            release.set()
            await app.workers.wait_for_complete()

    run_app(scenario)


def test_background_run_recovers_after_execution_errors(monkeypatch):
    from anyql.engine import PayloadError
    from anyql.tui import session as session_module

    started = threading.Event()
    release = threading.Event()
    execute = session_module.execute

    def failing_execute(*args, **kwargs):
        started.set()
        if not release.wait(5):
            raise RuntimeError("error run was not released")
        raise PayloadError("source temporarily unavailable")

    monkeypatch.setattr(session_module, "execute", failing_execute)

    async def scenario(app, pilot):
        app.editor.load_text(TYPED_DOCUMENT)
        try:
            await pilot.press("ctrl+enter")
            assert await asyncio.to_thread(started.wait, 2)
            await pilot.press("left")
            assert app.run_busy
            release.set()
            await app.workers.wait_for_complete()
            assert not app.run_busy and not app.session.busy
            assert "source temporarily unavailable" in text_of(app, "#results-error")
            assert not app.session.history

            monkeypatch.setattr(session_module, "execute", execute)
            await pilot.press("ctrl+enter")
            await app.workers.wait_for_complete()
            assert text_of(app, "#results-error") == ""
            assert results_table(app).row_count == 3
            assert [entry.doc for entry in app.session.history] == [TYPED_DOCUMENT]
        finally:
            release.set()
            await app.workers.wait_for_complete()

    run_app(scenario)


def test_quit_does_not_cancel_a_pending_temp_write_or_render_after_unmount(monkeypatch):
    from anyql.tui import session as session_module

    started = threading.Event()
    release = threading.Event()
    materialize = session_module.materialize

    def delayed_materialize(*args, **kwargs):
        started.set()
        if not release.wait(5):
            raise RuntimeError("write was not released after quitting")
        return materialize(*args, **kwargs)

    monkeypatch.setattr(session_module, "materialize", delayed_materialize)

    async def scenario():
        app = AnyqlApp()
        document = "\\from users\n\\select user_id\n\\limit 2\n\\temp kept"
        try:
            async with app.run_test(size=(140, 45)) as pilot:
                app.editor.load_text(document)
                await pilot.press("ctrl+enter")
                assert await asyncio.to_thread(started.wait, 2)
                pending = app._run_task
                await pilot.press("ctrl+q")
            assert app.run_busy
            release.set()
            outcome = await asyncio.wait_for(asyncio.shield(pending), 5)
            assert outcome.ok
            assert not app.run_busy and not app.session.busy
            assert app.session.source.datasets["kept"]["rows"] == 2
            assert [entry.doc for entry in app.session.history] == [document]
        finally:
            release.set()

    asyncio.run(scenario())


def test_a_typed_document_merges_two_tables_and_compiles_the_merge():
    """A `\\union all` document runs and renders its merge in SQL too."""

    async def scenario(app, pilot):
        app.editor.load_text("")
        await type_document(
            pilot,
            "\\with big\n"
            "  \\from events\n"
            "\\from big\n"
            "\\select user_id\n"
            "\\union all users\n"
            "\\order user_id\n"
            "\\limit 2",
        )
        await pilot.press("ctrl+enter")
        await app.workers.wait_for_complete()

        table = results_table(app)
        assert text_of(app, "#results-error") == ""
        assert table.row_count == 2
        # 100 events + 25 users, ascending: the smallest id twice.
        assert [str(table.get_cell_at((row, 0))) for row in range(2)] == ["1", "1"]
        assert text_of(app, "#results-status").startswith("2 rows · ")

        await pilot.press("ctrl+k")
        assert "UNION ALL" in app.query_one("#sql-text", TextArea).text

    run_app(scenario)


def test_a_parser_error_shows_and_does_not_execute():
    """A settled bad line stops the run at the parser, with its message."""

    async def scenario(app, pilot):
        app.editor.load_text("\\nope\n\\from events")
        await pilot.press("ctrl+enter")
        await app.workers.wait_for_complete()

        message = 'line 1: unknown command "\\nope"'
        assert text_of(app, "#results-error") == message
        assert text_of(app, "#status") == f"not executed · {message}"
        assert results_table(app).row_count == 0
        assert history_table(app).row_count == 0

    run_app(scenario)


def test_compile_renders_sql_for_the_chosen_dialect_without_executing():
    """Compiling fills the SQL pane, and the dialect select re-renders it."""

    async def scenario(app, pilot):
        app.editor.load_text(TYPED_DOCUMENT)
        await pilot.press("ctrl+k")

        sql = app.query_one("#sql-text", TextArea).text
        assert sql.upper().startswith("SELECT")
        assert "user_id" in sql
        assert results_table(app).row_count == 0  # compiling never executes
        assert history_table(app).row_count == 0
        assert app.query_one("#result-tabs").active == "tab-sql"
        assert text_of(app, "#status") == f"compiled for duckdb · {len(sql.splitlines())} lines"

        app.query_one("#dialect-select", Select).value = "mysql"
        await pilot.pause()
        assert "`user_id`" in app.query_one("#sql-text", TextArea).text
        assert text_of(app, "#status") == "dialect mysql"

        # A dialect this build knows but cannot render says so instead of crashing.
        app.query_one("#dialect-select", Select).value = "pyspark"
        await pilot.pause()
        assert "does not compile" in text_of(app, "#status")
        await pilot.press("ctrl+enter")
        await app.workers.wait_for_complete()
        assert results_table(app).row_count == 0
        assert "does not compile" in text_of(app, "#status")

    run_app(scenario)


# ---------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------


def test_add_source_modal_masks_credentials_and_cancels():
    """The form is there, the token is masked, and Cancel registers nothing."""

    async def scenario(app, pilot):
        await pilot.press("ctrl+o")
        modal = app.screen
        assert isinstance(modal, AddSourceModal)
        assert modal.query_one("#api-token", Input).password is True

        modal.query_one("#account-id", Input).value = "00000000000000000000000000000000"
        modal.query_one("#api-token", Input).value = "secret-token"
        modal.query_one("#cancel", Button).scroll_visible(immediate=True)
        await pilot.pause()
        await pilot.click("#cancel")

        assert not isinstance(app.screen, AddSourceModal)
        assert list(app.session.sources) == ["demo", "postgres", "mysql", "snowflake", "bigquery"]
        assert "secret-token" not in text_of(app, "#status")

    run_app(scenario)


def test_add_source_registers_a_local_snapshot(snapshot):
    """`Add` on a local snapshot registers it and re-points the whole app."""

    def make_scenario(path):
        async def scenario(app, pilot):
            await pilot.press("ctrl+o")
            app.screen.query_one("#snapshot-path", Input).value = path
            app.screen.query_one("#display-name", Input).value = "Widget DB"
            app.screen.query_one("#add", Button).scroll_visible(immediate=True)
            await pilot.pause()
            await pilot.click("#add")
            await app.workers.wait_for_complete()
            await pilot.pause()

            assert not isinstance(app.screen, AddSourceModal)
            assert list(app.session.sources) == [
                "demo",
                "postgres",
                "mysql",
                "snowflake",
                "bigquery",
                "widget-db",
            ]
            assert app.query_one("#source-select", Select).value == "widget-db"
            assert [str(node.label) for node in app.query_one("#schema-tree").root.children] == [
                "widgets  3 rows"
            ]
            assert [table.name for table in schema_tables()] == ["widgets"]
            assert text_of(app, "#backend-pill") == "sqlite (D1 snapshot)"
            assert app.session.dialect == "sqlite"

            app.editor.load_text("\\from widgets\n\\select name\n\\select id\n\\order id")
            await pilot.press("ctrl+enter")
            await app.workers.wait_for_complete()
            assert results_table(app).row_count == 3
            assert str(results_table(app).get_cell_at((0, 0))) == "row-0"

        return scenario

    run_app(make_scenario(snapshot("widgets", 3, table="widgets")))


def test_switching_source_repoints_the_schema_seam():
    """The tree, the seam and the backend pill follow the datasource select."""

    async def scenario(app, pilot):
        app.query_one("#source-select", Select).value = "postgres"
        await pilot.pause()

        assert [str(node.label) for node in app.query_one("#schema-tree").root.children] == [
            "customers  20 rows",
            "orders  120 rows",
        ]
        assert [table.name for table in schema_tables()] == ["customers", "orders"]
        assert capabilities().backend == "postgres (mock)"
        assert text_of(app, "#backend-pill") == "postgres (mock)"
        assert app.session.dialect == "postgres"
        assert app.query_one("#dialect-select", Select).value == "postgres"

        app.editor.load_text("\\from orders\n\\select status\n\\limit 2")
        await pilot.press("ctrl+enter")
        await app.workers.wait_for_complete()
        assert results_table(app).row_count == 2
        assert "· postgres · postgres" in text_of(app, "#results-status")

        app.query_one("#source-select", Select).value = "demo"
        await pilot.pause()
        assert [table.name for table in schema_tables()] == ["events", "users"]

    run_app(scenario)


# ---------------------------------------------------------------------------
# The preview cap
# ---------------------------------------------------------------------------


def test_preview_cap_bounds_the_results_buffer(snapshot):
    """A result longer than the cap is buffered to the cap, and says so."""

    async def scenario(app, pilot):
        assert PREVIEW_ROW_CAP == 10000

        # The demo's own 100 rows: nothing to cap, and the count agrees.
        app.editor.load_text("\\from events")
        await pilot.press("ctrl+enter")
        await app.workers.wait_for_complete()
        assert results_table(app).row_count == min(100, PREVIEW_ROW_CAP) == 100
        assert "preview capped" not in text_of(app, "#results-status")

        # A source with more rows than the buffer holds.
        app.session.register(add_sqlite_source("big", path, "Big snapshot"))
        app.query_one("#source-select", Select).set_options(app._source_options())
        app.query_one("#source-select", Select).value = "big"
        app.editor.load_text("\\from readings")
        await pilot.press("ctrl+enter")
        await app.workers.wait_for_complete()

        table = results_table(app)
        assert table.row_count == PREVIEW_ROW_CAP
        assert table.row_count == min(12000, PREVIEW_ROW_CAP)
        assert str(table.get_cell_at((PREVIEW_ROW_CAP - 1, 0))) == "9999"
        status = text_of(app, "#results-status")
        assert status.startswith("10000 rows · ")
        assert status.endswith("· showing 10000 of 12000 (preview capped)")
        assert str(history_table(app).get_cell_at((0, 3))) == "12000"

    path = snapshot("big", 12000)
    run_app(scenario)


# ---------------------------------------------------------------------------
# The palette
# ---------------------------------------------------------------------------


def test_palette_lists_commands_actions_and_inserts():
    """`\\` lists the language and the app's actions; Enter, Escape, click work."""

    async def scenario(app, pilot):
        app.editor.load_text("")
        await pilot.press("\\")

        labels = app.palette.view.labels
        for command in (
            "from",
            "open",
            "join",
            "union",
            "intersect",
            "except",
            "select",
            "where",
            "group",
            "order",
            "case",
            "limit",
            "with",
        ):
            assert f"\\{command}" in labels

        # An abbreviation is completed in place, and the offers follow it into
        # the clause it opened.
        app.editor.load_text("")
        await pilot.pause()
        await type_document(pilot, "\\sel")
        assert app.palette.view.labels == ["\\select"]  # only the match survives
        assert app.palette.highlighted == 0
        await pilot.press("enter")
        assert app.editor.text == "\\select "
        assert app.palette.is_open is True  # the offers follow it to the new clause

        # Typed in full, Enter is a newline: accepting it would change nothing.
        app.editor.load_text("")
        await pilot.pause()
        await type_document(pilot, "\\select")
        assert app.palette.view.labels == ["\\select"]
        await pilot.press("enter")
        assert app.editor.text == "\\select\n"

        # Escape closes without touching the document.
        await type_document(pilot, "\\where")
        assert app.palette.is_open is True
        await pilot.press("escape")
        assert app.palette.is_open is False
        assert app.editor.text == "\\select\n\\where"

        # A click accepts the row it lands on.
        await pilot.press("enter")
        await type_document(pilot, "\\")
        await pilot.pause()
        assert app.palette.option_count == len(app.palette.view.entries)
        await pilot.click("#palette", offset=(3, 2))  # the second command
        assert app.editor.text == "\\select\n\\where\n\\open "
        assert app.focused is app.editor

    run_app(scenario)


def test_palette_follows_the_caret_and_the_schema():
    """Navigation moves the highlight, not the caret, and columns come live."""

    async def scenario(app, pilot):
        app.editor.load_text("\\from events\n")
        app.editor.cursor_location = (1, 0)
        await type_document(pilot, "\\select ")

        caret = app.editor.cursor_location
        await pilot.press("down", "down")
        assert app.palette.highlighted == 2
        assert app.editor.cursor_location == caret  # the caret stays put
        await pilot.press("up")
        assert app.palette.highlighted == 1

        await pilot.press("enter")
        assert app.editor.text == "\\from events\n\\select user_id "
        assert app.palette.is_open is True  # accepting keeps the offers on
        assert app.palette.view.labels == ["user_id"]  # the field it just landed on

        # `\where` walks column → operator → that column's distinct values.
        await pilot.press("escape")  # the offers stay on only until Escape says so
        await pilot.press("enter")  # the clause gets its own line, as it would
        await type_document(pilot, "\\where event_type")
        assert app.palette.view.labels == ["event_type"]
        await type_document(pilot, " ")
        assert "=" in app.palette.view.labels
        await type_document(pilot, "= ")
        values = app.palette.view.labels
        assert values == ["click", "purchase", "signup", "view"]  # from the engine
        await type_document(pilot, "pu")
        await pilot.press("enter")
        assert app.editor.text.endswith('\\where event_type = "purchase" ')

        # Moving the caret onto a completable spot does not summon anything —
        # the arrows stay the document's own — but the next keystroke does.
        app.editor.cursor_location = (1, len("\\select user_id "))
        await pilot.pause()
        assert app.palette.is_open is False
        await type_document(pilot, ", ")
        assert app.palette.is_open is True
        assert app.palette.view.labels[:2] == ["timestamp", "user_id"]

    run_app(scenario)


def test_the_palette_view_picks_its_span_and_its_offers():
    """The palette's rules are testable without a terminal at all."""
    session = Session()
    doc = "\\from events\n\\select "

    command = view_for(session, "\\", "\\", 1)
    assert command.start == 0  # an accept replaces the typed backslash
    assert command.token == ""
    assert command.phase == "command"
    assert "\\from" in command.labels

    columns = view_for(session, doc, "\\select us", len("\\select us"))
    assert columns.labels == ["user_id"]
    assert columns.start == len("\\select ")  # replaces just the typed argument
    assert columns.phase == "argument"

    assert view_for(session, doc, "\\select ", len("\\select ")).labels[:2] == ["timestamp", "user_id"]
    assert view_for(session, "\\limit ", "\\limit ", len("\\limit ")) is None
    assert view_for(session, "no command here", "no command here", 6) is None

    # The dataset offer stops once the clause names one: `\from events |` takes
    # an alias, not a second table, so no stray Enter can append one.
    assert view_for(session, "\\from ev", "\\from ev", len("\\from ev")).labels == ["events"]
    assert view_for(session, "\\from events ", "\\from events ", len("\\from events ")) is None
    assert view_for(session, "\\join ", "\\join ", len("\\join ")).labels  # still a choice
    joined = "\\join users on user_id "
    assert view_for(session, joined, joined, len(joined)) is None

    # A `\where` takes one value: offered until the value is closed, gone after.
    open_value = "\\where event_type = "
    assert view_for(session, open_value, open_value, len(open_value)).labels
    half = '\\where event_type = "pur'
    assert view_for(session, half, half, len(half)).labels == ["purchase"]
    done = '\\where event_type = "purchase" '
    assert view_for(session, done, done, len(done)) is None


def test_set_op_offers_the_documents_tables_and_its_modifier():
    """`\\union` completes a table name, and spells out `all`/`distinct` once."""
    session = Session()
    doc = '\\with recent\n  \\from events\n\\from recent\n\\union '

    # The document's own CTE leads the source's datasets.
    names = view_for(session, doc, "\\union ", len("\\union "))
    assert names.labels[:2] == ["recent", "events"]
    assert names.phase == "argument"

    # The modifier leads the argument, and is done offering once it is there.
    assert view_for(session, doc, "\\union al", len("\\union al")).labels == ["all"]
    after = view_for(session, doc, "\\union all ", len("\\union all "))
    assert after.labels[:2] == ["recent", "events"]
    assert "all" not in after.labels

    # `\union events |` names a table: the clause takes nothing more.
    done = "\\union events "
    assert view_for(session, doc, done, len(done)) is None

    commands = view_for(session, "\\", "\\", 1).labels
    assert "\\union" in commands and "\\intersect" in commands and "\\except" in commands


def test_suggestions_match_inside_a_name_not_only_at_its_start():
    """`id` finds `user_id`, which the old prefix rule hid — the web IDE's rule."""
    session = Session()
    doc = "\\from events\n\\select id"
    assert view_for(session, doc, "\\select id", len("\\select id")).labels == ["user_id"]

    # An exact or prefix hit still leads a hit further inside the name.
    ranked = view_for(session, "\\from events\n\\select am", "\\select am", len("\\select am")).labels
    assert ranked[:2] == ["amount", "timestamp"]  # `am` opens `amount`, `t-a-m-p` follows


def test_where_values_search_the_pool_not_just_the_first_page(snapshot):
    """The popup shows 50 values, but typing searches every value it fetched."""
    source = add_sqlite_source("wide", snapshot("wide", 300), "Wide snapshot")
    session = Session({"wide": source})
    doc = "\\from readings\n\\where name = "

    everything = view_for(session, doc, "\\where name = ", len("\\where name = "))
    assert len(everything.labels) == VALUE_SUGGESTIONS == 50
    assert VALUE_POOL_LIMIT > VALUE_SUGGESTIONS
    first_page = set(everything.labels)
    assert "row-299" not in first_page  # sorting puts it past the shown 50

    found = view_for(session, doc, "\\where name = row-299", len("\\where name = row-299"))
    assert found.labels == ["row-299"]

    # A quoted partial searches the same way, and inserts the quoted value.
    quoted = view_for(session, doc, '\\where name = "row-29', len('\\where name = "row-29'))
    assert "row-299" in quoted.labels
    assert quoted.entries[0].insert.startswith('"row-29')


def test_intellisense_escape_shows_and_hides_the_popup():
    """Escape closes the popup and it stays shut; Escape again brings it back."""

    async def scenario(app, pilot):
        app.editor.load_text("\\from events\n")
        app.editor.cursor_location = (1, 0)
        await type_document(pilot, "\\where event_type = pu")
        assert app.palette.view.labels == ["purchase"]

        await pilot.press("escape")
        assert app.palette.is_open is False
        await type_document(pilot, "r")
        assert app.palette.is_open is False  # typing does not undo the dismissal
        assert app.editor.text.endswith("\\where event_type = pur")

        await pilot.press("escape")
        assert app.palette.is_open is True  # the same key shows it again
        assert app.palette.view.labels == ["purchase"]
        await pilot.press("enter")
        assert app.editor.text.endswith('\\where event_type = "purchase" ')
        # The clause now carries its value: nothing left to offer, so Enter is
        # a newline again without an Escape first.
        assert app.palette.is_open is False
        await pilot.press("enter")
        assert app.editor.text.endswith('\\where event_type = "purchase" \n')

        # Nothing to summon at a done clause, and Escape leaves it that way; a
        # fresh `\` is still the summon that brings the command list back.
        await pilot.press("escape")
        assert app.palette.is_open is False
        await type_document(pilot, "\\")
        assert app.palette.is_open is True
        assert "\\from" in app.palette.view.labels
        await pilot.press("escape")
        assert app.palette.is_open is False

    run_app(scenario)


def test_select_offers_functions_and_completes_their_argument():
    """`\\select` offers the aggregates, and `sum(` completes to `sum(amount)`."""

    async def scenario(app, pilot):
        app.editor.load_text("\\from events\n")
        app.editor.cursor_location = (1, 0)
        await type_document(pilot, "\\select su")
        await pilot.press("enter")
        assert app.editor.text == "\\from events\n\\select sum("
        assert app.palette.is_open is True  # an open call keeps offering
        assert app.palette.view.labels[0] == "timestamp"  # every column applies

        await type_document(pilot, "am")
        assert app.palette.view.labels == ["amount", "timestamp"]  # prefix leads
        assert app.palette.view.entries[0].insert == "amount)"
        await pilot.press("enter")
        assert app.editor.text == "\\from events\n\\select sum(amount)"

        # A temporal extraction only takes the dtype family it applies to.
        await pilot.press("enter")
        await type_document(pilot, "\\select year(")
        assert app.palette.view.labels == ["timestamp"]

    run_app(scenario)


def test_string_completion_tracks_nested_arguments_and_quoted_commas():
    session = Session()
    doc = "\\from events e\n"
    prefix = "\\select concat(upper(e.event_type), 'it''s, (text)', "
    line = prefix + "pa"
    view = view_for(session, doc + line, line, len(line))
    assert line[:view.start] + view.entries[0].insert == prefix + "path"

    line = "\\select substr(e.path, "
    view = view_for(session, doc + line, line, len(line))
    assert "user_id" in view.labels
    assert "path" not in view.labels
    assert "amount" not in view.labels
    assert "length" in view.labels
    assert "upper" not in view.labels

    line = "\\select concat('unfinished, "
    assert view_for(session, doc + line, line, len(line)) is None

    line = "\\select concat(e.path, string(us"
    view = view_for(session, doc + line, line, len(line))
    assert line[:view.start] + view.entries[0].insert == "\\select concat(e.path, string(user_id)"


def test_string_completion_obeys_source_capabilities():
    session = Session()
    caps = replace(capabilities(), functions={"string": ["upper"], "any": ["string"]})
    set_schema_state(list(schema_tables()), caps)
    line = "\\select "
    view = view_for(session, "\\from events\n" + line, line, len(line))
    assert "upper" in view.labels
    assert "concat" not in view.labels
    line = "\\select concat("
    assert view_for(session, "\\from events\n" + line, line, len(line)) is None


def test_string_calls_complete_and_execute_in_the_editor():
    async def scenario(app, pilot):
        app.editor.load_text("\\from events\n")
        app.editor.cursor_location = (1, 0)
        await type_document(pilot, "\\select concat")
        await pilot.press("enter")
        await type_document(pilot, "upp")
        await pilot.press("enter")
        await type_document(pilot, "event_ty")
        await pilot.press("enter")
        await type_document(pilot, ", ':', stri")
        await pilot.press("enter")
        await type_document(pilot, "user_i")
        await pilot.press("enter")
        await type_document(pilot, ") as label\n\\limit 2")
        assert app.editor.text == (
            "\\from events\n\\select concat(upper(event_type), ':', string(user_id)) as label\n\\limit 2"
        )
        await pilot.press("ctrl+enter")
        await app.workers.wait_for_complete()
        assert text_of(app, "#results-error") == ""
        source = app.session.source.datasets["events"]["table"].limit(2).execute()
        expected = [f"{row.event_type.upper()}:{row.user_id}" for row in source.itertuples()]
        table = results_table(app)
        assert [str(table.get_cell_at((row, 0))) for row in range(table.row_count)] == expected

    run_app(scenario)


def test_clause_commands_take_their_own_line_or_the_line_they_have():
    """Accepting `\\where` starts a line for it — or lands on the one it has."""

    async def scenario(app, pilot):
        # Mid-line: the command breaks the line instead of landing in it.
        app.editor.load_text("\\from events")
        app.editor.cursor_location = (0, len("\\from events"))
        await type_document(pilot, "\\sel")
        assert app.palette.view.labels == ["\\select"]
        await pilot.press("enter")
        assert app.editor.text == "\\from events\n\\select "
        assert app.editor.cursor_location == (1, len("\\select "))

        # The block already carries the clause: the caret goes to it, and the
        # document is left exactly as it was.
        app.editor.load_text("\\from events\n\\select user_id\n\\where amount > 1")
        app.editor.cursor_location = (2, len("\\where amount > 1"))
        await type_document(pilot, "\\sel")
        assert app.palette.view.labels == ["\\select"]
        await pilot.press("enter")
        assert app.editor.text == "\\from events\n\\select user_id\n\\where amount > 1"
        assert app.editor.cursor_location == (1, len("\\select user_id"))
        # The jump is followed, not just made: the offers land at the clause too.
        assert app.palette.is_open is True
        assert app.palette.view.labels == ["user_id"]

        # Inside a CTE body the lookup stays in that block: the body's `\where`
        # is not the document's, so this one is written, not reused.
        app.editor.load_text('\\with recent\n  \\from orders\n  \\where status = "paid"\n\\from recent')
        app.editor.cursor_location = (3, len("\\from recent"))
        await type_document(pilot, "\\whe")
        assert app.palette.view.labels == ["\\where"]
        await pilot.press("enter")
        assert app.editor.text.endswith('\\from recent\n\\where ')
        assert app.editor.text.count("\\where") == 2  # the body's line is untouched
        assert app.editor.cursor_location == (4, len("\\where "))

    run_app(scenario)


# ---------------------------------------------------------------------------
# The panes
# ---------------------------------------------------------------------------


def test_pane_actions_show_and_hide_each_pane():
    """`\\results`, `\\sql` and `\\schema` toggle their pane and report it."""

    async def scenario(app, pilot):
        app.editor.load_text("")
        await type_document(pilot, "\\results")
        await pilot.press("enter")
        assert app.editor.text == ""  # an action never enters the document
        assert app.pane_visible("results") is False
        assert text_of(app, "#status") == "results pane hidden"

        await type_document(pilot, "\\sql")
        await pilot.press("enter")
        assert app.pane_visible("sql") is False

        await type_document(pilot, "\\schema")
        await pilot.press("enter")
        assert app.pane_visible("schema") is False
        assert text_of(app, "#status") == "schema pane hidden"

        await type_document(pilot, "\\RESULTS")
        await pilot.press("enter")
        assert app.pane_visible("results") is True
        assert app.query_one("#result-tabs", TabbedContent).active == "tab-results"
        assert text_of(app, "#status") == "results pane shown"

    run_app(scenario)


def test_a_hidden_pane_stays_hidden_when_the_document_runs():
    """Hiding a pane is the user's call; a run never forces it back."""

    async def scenario(app, pilot):
        app.editor.load_text("")
        await type_document(pilot, "\\results")
        await pilot.press("enter")
        assert app.pane_visible("results") is False

        app.editor.load_text(TYPED_DOCUMENT)
        await pilot.press("ctrl+enter")
        await app.workers.wait_for_complete()
        assert history_table(app).row_count == 1
        assert results_table(app).row_count == 3  # the run still rendered
        assert app.pane_visible("results") is False
        assert app.query_one("#result-tabs", TabbedContent).active != "tab-results"

    run_app(scenario)


def test_history_is_a_pane_and_an_empty_strip_frees_the_bench():
    """`\\history` toggles like the rest; with no tab left the document grows."""

    async def scenario(app, pilot):
        bench = app.query_one("#work-bench")
        editor_pane = app.query_one("#editor-pane")
        assert editor_pane.region.height < bench.region.height

        for name in ("results", "sql", "history"):
            await type_document(pilot, f"\\{name}")
            await pilot.press("enter")
            await pilot.pause()
            assert app.pane_visible(name) is False

        # No tab is left: the strip goes and the document takes the bench.
        assert app.query_one("#result-tabs", TabbedContent).display is False
        assert bench.has_class("no-tabs")
        assert editor_pane.region.height == bench.region.height

        await type_document(pilot, "\\history")
        await pilot.press("enter")
        await pilot.pause()
        assert app.pane_visible("history") is True
        assert app.query_one("#result-tabs", TabbedContent).display is True
        assert not bench.has_class("no-tabs")
        assert editor_pane.region.height < bench.region.height

    run_app(scenario)


def test_ctrl_a_selects_the_whole_document():
    """ctrl+a is select-all in the document, not TextArea's "go to line start"."""

    async def scenario(app, pilot):
        app.editor.load_text(TYPED_DOCUMENT)
        app.editor.cursor_location = (1, 4)
        await pilot.press("ctrl+a")
        assert app.editor.selected_text == TYPED_DOCUMENT

        # A select-all is meant to be typed over.
        await type_document(pilot, "\\limit 1")
        assert app.editor.text == "\\limit 1"

    run_app(scenario)


def test_tab_accepts_a_suggestion_and_never_moves_focus():
    """Tab is the accept key here; it does not walk focus between panes."""

    async def scenario(app, pilot):
        app.editor.load_text("\\from events\n")
        app.editor.cursor_location = (1, 0)
        await type_document(pilot, "\\select us")
        assert app.palette.view.labels == ["user_id"]

        await pilot.press("tab")
        assert app.editor.text == "\\from events\n\\select user_id "
        assert app.palette.is_open is True  # intellisense stays on past the accept
        assert app.palette.view.labels == ["user_id"]  # the field the caret landed on
        assert app.focused is app.editor

        # Nothing to accept: Tab still does not move focus anywhere.
        await pilot.press("escape")
        await pilot.press("tab")
        assert app.focused is app.editor

        table = results_table(app)
        table.focus()
        await pilot.pause()
        await pilot.press("tab")
        assert app.focused is table

    run_app(scenario)


def test_column_selection_inserts_into_the_editor():
    """Expanding a dataset loads its columns and inserting one lands in the doc."""

    async def scenario(app, pilot):
        tree = app.query_one("#schema-tree")
        node = tree.root.children[0]
        assert str(node.children[0].label) == "…"  # unloaded until expanded
        node.expand()
        await pilot.pause()
        assert [str(child.label) for child in node.children] == [
            "timestamp  timestamp",
            "user_id  int64",
            "event_type  string",
            "amount  float64",
            "path  string",
        ]

        app.editor.load_text("\\from events\n\\select ")
        app.editor.cursor_location = (1, len("\\select "))
        node.expand()  # re-expanding must not duplicate the columns
        assert [str(child.label) for child in node.children][3] == "amount  float64"

        tree.focus()
        tree.select_node(node.children[3])
        await pilot.pause()
        assert app.editor.text == "\\from events\n\\select amount"
        assert app.focused is app.editor

    run_app(scenario)


def test_history_loads_a_document_back():
    """A run lands in History, and selecting it restores the document."""

    async def scenario(app, pilot):
        app.editor.load_text(TYPED_DOCUMENT)
        await pilot.press("ctrl+enter")
        await app.workers.wait_for_complete()
        assert history_table(app).row_count == 1

        app.editor.load_text("")
        await pilot.pause()
        table = history_table(app)
        table.focus()
        table.move_cursor(row=0, column=0)
        await pilot.press("enter")

        assert app.editor.text == TYPED_DOCUMENT
        assert app.focused is app.editor

    run_app(scenario)


def test_results_table_takes_the_keys_it_needs():
    """The explorer keeps its cursor, resizes columns, and spares the editor."""

    async def scenario(app, pilot):
        app.editor.load_text("\\from events\n\\select user_id\n\\select amount\n\\limit 5")
        await pilot.press("ctrl+enter")
        await app.workers.wait_for_complete()

        table = results_table(app)
        assert [str(column.label) for column in table.ordered_columns] == [
            "user_id · int64",
            "amount · float64",
        ]
        table.focus()
        await pilot.pause()
        await pilot.press("down", "down", "right")
        assert (table.cursor_row, table.cursor_column) == (2, 1)

        amount = table.ordered_columns[1]
        widths = [amount.get_render_width(table)]
        for key in ("shift+right", "shift+right", "shift+left"):
            await pilot.press(key)
            widths.append(amount.get_render_width(table))
        assert widths == [widths[0], widths[0] + 2, widths[0] + 4, widths[0] + 2]

        # Enter belongs to the table here, not to the document behind it.
        document = app.editor.text
        await pilot.press("enter")
        assert app.editor.text == document
        assert app.focused is table

        # The app's own binding still runs the document from this focus.
        await pilot.press("ctrl+enter")
        await app.workers.wait_for_complete()
        assert results_table(app).row_count == 5
        assert app.editor.text == document

    run_app(scenario)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


async def open_settings(app, pilot) -> SettingsScreen:
    """Type `\\settings` and accept it, the way the user reaches the menu."""
    await type_document(pilot, "\\settings")
    assert app.palette.view.labels == ["Settings"]
    await pilot.press("enter")
    await pilot.pause()
    screen = app.screen
    assert isinstance(screen, SettingsScreen)
    return screen


async def pick(pilot, app, match: str) -> None:
    """Select a category or an action in the Settings workspace."""
    screen = app.screen
    assert isinstance(screen, SettingsScreen)
    sidebar = screen.query_one("#settings-sidebar", OptionList)
    for index in range(sidebar.option_count):
        option = sidebar.get_option_at_index(index)
        if match in (str(option.prompt), option.id):
            sidebar.focus()
            sidebar.highlighted = index
            await pilot.pause()
            await pilot.press("enter")
            return
    menu = screen.query_one("#settings-menu", OptionList)
    menu.focus()
    menu.highlighted = next(
        index for index, row in enumerate(screen.rows) if match in (row.label, row.value)
    )
    await pilot.press("enter")
    await pilot.pause()


def test_settings_sidebar_preserves_category_when_returning_from_details():
    """Browsing categories previews controls; only accepting a control changes it."""

    async def scenario(app, pilot):
        screen = await open_settings(app, pilot)
        sidebar = screen.query_one("#settings-sidebar", OptionList)
        details = screen.query_one("#settings-menu", OptionList)
        assert sidebar.has_focus
        assert sidebar.region.right < details.region.x
        await pilot.press("down")
        assert screen.menu == "menus"
        assert sidebar.has_focus
        assert all(app.pane_visible(name) for name in app.PANE_TITLES)
        await pilot.press("enter")
        assert details.has_focus
        await pilot.press("escape")
        assert sidebar.has_focus
        assert screen.menu == "menus"
        await pilot.press("escape")
        assert not isinstance(app.screen, SettingsScreen)
        assert app.focused is app.editor

    run_app(scenario)


def test_settings_menus_submenu_flips_panes_at_once():
    """Show/Hide Menus lists the three panes and hides one on the spot."""

    async def scenario(app, pilot):
        screen = await open_settings(app, pilot)
        await pick(pilot, app, "Show/Hide Menus")
        assert [row.label for row in screen.rows] == [
            "Results pane",
            "SQL pane",
            "History pane",
            "Schema pane",
        ]
        assert [row.detail for row in screen.rows] == ["visible"] * 4

        await pick(pilot, app, "SQL pane")
        assert app.pane_visible("sql") is False
        assert screen.rows[1].detail == "hidden"

        await pick(pilot, app, "SQL pane")
        assert app.pane_visible("sql") is True
        assert screen.rows[1].detail == "visible"

        await pilot.press("escape")
        assert screen.query_one("#settings-sidebar", OptionList).has_focus
        await pilot.press("escape")
        assert not isinstance(app.screen, SettingsScreen)

    run_app(scenario)


def test_settings_intellisense_switch_is_the_hard_off():
    """Off means nothing opens, and `ctrl+comma` is the way back to the switch."""

    async def scenario(app, pilot):
        screen = await open_settings(app, pilot)
        await pick(pilot, app, "Intellisense")
        assert app.session.intellisense is False
        assert [row.detail for row in screen.rows if row.label == "Intellisense"] == ["off"]
        await pilot.press("escape", "escape")

        app.editor.load_text("\\from events\n")
        app.editor.cursor_location = (1, 0)
        await type_document(pilot, "\\select us")
        assert app.palette.is_open is False  # no arguments while it is off
        await type_document(pilot, "\n\\")
        assert app.palette.is_open is False  # not even the command list

        await pilot.press("ctrl+comma")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)
        await pick(pilot, app, "Intellisense")
        assert app.session.intellisense is True
        await pilot.press("escape", "escape")
        assert not isinstance(app.screen, SettingsScreen)

        app.editor.load_text("\\from events\n")
        app.editor.cursor_location = (1, 0)
        await type_document(pilot, "\\select us")
        assert app.palette.view.labels == ["user_id"]

    run_app(scenario)


def test_settings_keybindings_come_from_the_running_widgets():
    """The reference is read off the bindings the app and its panes declare."""

    async def scenario(app, pilot):
        screen = await open_settings(app, pilot)
        await pick(pilot, app, "Keybindings")
        rows = [(row.label, row.detail) for row in screen.rows]
        assert ("ctrl+enter", "Run · app") in rows
        assert ("ctrl+k", "Compile · app") in rows
        assert ("ctrl+o", "Data source · app") in rows
        assert ("ctrl+comma", "Settings · app") in rows
        assert ("escape", "Close the palette · document") in rows
        assert ("shift+left", "Narrow column · results") in rows
        assert ("escape", "Back · settings") in rows
        assert ("tab", "Accept suggestion · document") in rows
        assert ("ctrl+q", "Quit · textual") in rows
        assert all(row.action == "" for row in screen.rows)  # a reference, not a menu
        assert not any(label == "ctrl+c" for label, _ in rows)  # internal aliases stay out
        assert not any(detail.startswith("Focus ") for _, detail in rows)  # tab walks nothing

    run_app(scenario)


def test_settings_switches_the_source_and_the_one_dialect():
    """Dialect and Data source rows drive the same state the header selects do."""

    async def scenario(app, pilot):
        screen = await open_settings(app, pilot)

        await pick(pilot, app, "Dialect")
        assert len(screen.rows) == len(DIALECTS)
        by_name = {row.value: row for row in screen.rows}
        assert by_name["duckdb"].detail == "active"
        assert by_name["pyspark"].detail == "known but does not compile in this build"

        await pick(pilot, app, "mysql")
        assert app.session.dialect == "mysql"
        assert {row.value: row.detail for row in screen.rows}["mysql"] == "active"
        assert app.query_one("#dialect-select", Select).value == "mysql"

        await pilot.press("escape")
        await pick(pilot, app, "Data source")
        await pick(pilot, app, "postgres")
        assert app.session.active_id == "postgres"
        assert app.query_one("#source-select", Select).value == "postgres"
        assert [table.name for table in schema_tables()] == ["customers", "orders"]
        assert text_of(app, "#backend-pill") == "postgres (mock)"

    run_app(scenario)


# ---------------------------------------------------------------------------
# The server is gone
# ---------------------------------------------------------------------------

FORBIDDEN = frozenset(
    {
        "aiohttp",
        "bottle",
        "fastapi",
        "flask",
        "http.server",
        "socketserver",
        "starlette",
        "tornado",
        "uvicorn",
        "werkzeug",
        "wsgiref",
    }
)


def imported_modules(path: Path) -> set[str]:
    """Top-level modules a source file imports, read structurally."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_the_tui_reaches_no_http_server():
    """No TUI module imports a server, and importing the app brings none in."""
    offenders = {}
    for path in sorted(TUI_DIR.glob("*.py")):
        hits = sorted(FORBIDDEN & imported_modules(path))
        if hits:
            offenders[path.name] = hits
    assert offenders == {}

    import anyql.tui  # noqa: F401  (imported for its side effect on sys.modules)

    loaded = {name.split(".")[0] for name in sys.modules}
    assert not (FORBIDDEN & loaded)


# ---------------------------------------------------------------------------
# The statements the query cannot carry: temp tables and transactions
# ---------------------------------------------------------------------------


def test_the_palette_offers_the_statement_commands(snapshot):
    """Every command is in the palette, and only `\\drop` has a list to offer."""
    session = Session()
    labels = view_for(session, "\\", "\\", 1).labels
    for command in ("\\temp", "\\drop", "\\begin", "\\commit", "\\rollback"):
        assert command in labels
    # Savepoints are the engine's own gift: DuckDB has none, so they are not
    # offered on it — and a SQLite snapshot, which has them, offers both.
    assert "\\savepoint" not in labels and "\\release" not in labels
    assert view_for(session, "\\nope ", "\\nope ", len("\\nope ")) is None

    # A temp table's name is typed, not chosen; the ones there are, are offered.
    assert view_for(session, "\\temp ", "\\temp ", len("\\temp ")) is None
    assert view_for(session, "\\drop ", "\\drop ", len("\\drop ")) is None  # nothing yet
    session.run("\\from users\n\\select user_id\n\\limit 2\n\\temp few\n")
    drop = view_for(session, "\\drop ", "\\drop ", len("\\drop "))
    assert drop.labels == ["few"]
    assert drop.entries[0].detail == "temp table"

    # A second session re-points the shared schema seam, so it goes last.
    snap = Session({"snap": add_sqlite_source("snap", snapshot("snap", 3), "Snap")})
    snap_labels = view_for(snap, "\\", "\\", 1).labels
    assert "\\savepoint" in snap_labels and "\\release" in snap_labels


def test_a_where_operand_that_is_typed_offers_nothing():
    """`~`, `!~`, `in` and `not in` take patterns and subqueries, not values."""
    session = Session()
    for line in ("\\where path ~ ", "\\where path !~ ", "\\where user_id in ", "\\where user_id not in "):
        assert view_for(session, line, line, len(line)) is None
    # The operators themselves are offered, and `like` still offers values.
    operators = view_for(session, "\\where path ", "\\where path ", len("\\where path ")).labels
    assert "~" in operators and "in" in operators
    like = '\\where event_type like '
    assert view_for(session, like, like, len(like)).labels


def test_a_typed_document_keeps_a_temp_table_and_drops_it_again():
    """`\\temp` runs, lands in the explorer, and `\\drop` takes it away."""

    async def scenario(app, pilot):
        app.editor.load_text("")
        await type_document(pilot, "\\from users\n\\select user_id\n\\limit 3\n\\temp few")
        await pilot.press("ctrl+enter")
        await app.workers.wait_for_complete()

        assert text_of(app, "#results-error") == ""
        assert results_table(app).row_count == 3
        assert text_of(app, "#results-status").startswith('created temp table "few" · 3 rows · ')
        # The table joined the source, and the explorer says it is a temp one.
        assert "few" in app.session.source.datasets
        assert app.session.source.datasets["few"]["temp"] is True
        labels = [str(node.label) for node in app.query_one("#schema-tree").root.children]
        assert "few  3 rows  temp" in labels

        # A later document reads it back by name, and `\drop` removes it.
        app.editor.load_text("\\from few\n\\select user_id")
        await pilot.press("ctrl+enter")
        await app.workers.wait_for_complete()
        assert results_table(app).row_count == 3

        app.editor.load_text("\\drop few")
        await pilot.press("ctrl+enter")
        await app.workers.wait_for_complete()
        assert text_of(app, "#results-status") == 'dropped temp table "few"'
        assert [str(node.label) for node in app.query_one("#schema-tree").root.children] == [
            "events  100 rows",
            "users  25 rows",
        ]

    run_app(scenario)


def test_a_transaction_statement_reports_itself_and_survives_a_query():
    """`\\begin` is a document of its own; the query after it runs inside it."""

    async def scenario(app, pilot):
        app.editor.load_text("\\begin")
        await pilot.press("ctrl+enter")
        await app.workers.wait_for_complete()
        assert text_of(app, "#results-error") == ""
        assert text_of(app, "#status") == "transaction open · demo"
        assert app.session.tx_state().open is True

        app.editor.load_text("\\commit")
        await pilot.press("ctrl+enter")
        await app.workers.wait_for_complete()
        assert text_of(app, "#status") == "transaction committed · demo"
        assert app.session.tx_state().open is False

        # A refused statement is the results-tab line, exactly like a bad query.
        app.editor.load_text("\\rollback")
        await pilot.press("ctrl+enter")
        await app.workers.wait_for_complete()
        assert text_of(app, "#results-error") == "no transaction is open on demo — \\begin first"

    run_app(scenario)


def test_a_lateral_join_and_a_subquery_run_from_the_document():
    """The two new query shapes reach the engine through the app itself."""

    async def scenario(app, pilot):
        app.editor.load_text("")
        await type_document(
            pilot,
            "\\from users u\n"
            "\\join lateral (\\from events e \\where e.user_id = u.user_id \\order timestamp desc \\limit 2) as recent\n"
            "\\select u.user_id\n",
        )
        await pilot.press("ctrl+enter")
        await app.workers.wait_for_complete()
        assert text_of(app, "#results-error") == ""
        assert results_table(app).row_count == 50  # two events for each of 25 users

        app.editor.load_text(
            "\\from events\n"
            "\\select user_id\n"
            "\\where user_id in (\\from users \\select user_id \\where region = \"us\")\n"
        )
        await pilot.press("ctrl+enter")
        await app.workers.wait_for_complete()
        assert text_of(app, "#results-error") == ""
        assert results_table(app).row_count > 0
        await pilot.press("ctrl+k")
        assert "IN (" in app.query_one("#sql-text", TextArea).text

    run_app(scenario)


# ---------------------------------------------------------------------------
# Table-valued functions
# ---------------------------------------------------------------------------


FN_BODY = "\\from events\n\\where amount > @min_amount\n\\select user_id, amount"


async def fill_fn(screen, *, name, params, body, doc="", args="") -> None:
    """Put a function into the library's fields, as a person would type them."""
    screen.query_one("#fn-name", Input).value = name
    screen.query_one("#fn-doc", Input).value = doc
    screen.query_one("#fn-params", Input).value = params
    screen.query_one("#fn-body", TextArea).text = body
    screen.query_one("#fn-args", Input).value = args


def test_fn_library_authors_and_previews_then_the_document_calls_it():
    """`\\fn` opens the library; a saved function previews and runs from the document."""

    async def scenario(app, pilot):
        await type_document(pilot, "\\fn")
        assert app.palette.view.labels == ["New function…"]  # empty library, one offer
        await pilot.press("enter")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, FnScreen)

        await fill_fn(
            screen,
            name="hot",
            params="min_amount",
            body=FN_BODY,
            doc="events above a threshold",
            args="0",
        )
        await pilot.click("#fn-preview")  # preview saves first, then runs the call
        await pilot.pause()
        assert list(app.session.fns) == ["hot"]
        assert screen.query_one("#fn-grid", DataTable).row_count == 100  # every event clears 0
        # a preview is a trial, not a run: History stays empty.
        assert app.session.history == []

        await pilot.press("ctrl+c")
        await pilot.pause()
        assert not isinstance(app.screen, FnScreen)
        assert app.focused is app.editor

        # The saved function is now a source the document can call.
        app.editor.load_text("\\from hot(0)\n\\select *")
        await pilot.press("ctrl+enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert text_of(app, "#results-error") == ""
        assert results_table(app).row_count == 100

    run_app(scenario)


def test_a_bad_body_is_refused_on_the_screen_not_at_the_call():
    """Saving a function whose `@param` is undeclared names it in the status, in red."""

    async def scenario(app, pilot):
        await type_document(pilot, "\\fn")
        await pilot.press("enter")
        await pilot.pause()
        screen = app.screen
        await fill_fn(screen, name="oops", params="min_amount", body=FN_BODY.replace("@min_amount", "@nope"))
        await pilot.click("#fn-save")
        await pilot.pause()
        status = screen.query_one("#fn-status", Static)
        assert "@nope" in str(status.content)
        assert status.has_class("error")
        assert "oops" not in app.session.fns

    run_app(scenario)


def test_fn_named_in_the_palette_opens_it_or_starts_a_new_one():
    """`\\fn <name>` opens an existing function, or a blank form named the new one."""

    async def scenario(app, pilot):
        app.session.save_fn("hot", "min_amount", FN_BODY, "above a threshold")
        await type_document(pilot, "\\fn hot")
        assert app.palette.view.labels == ["hot"]  # the exact name leads, nothing to create
        await pilot.press("enter")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, FnScreen)
        assert screen.query_one("#fn-name", Input).value == "hot"
        assert screen.query_one("#fn-params", Input).value == "min_amount"
        assert screen.query_one("#fn-body", TextArea).text == FN_BODY
        assert screen.query_one("#fn-doc", Input).value == "above a threshold"
        await pilot.press("ctrl+c")
        await pilot.pause()

        # A name the library lacks starts a new function, prefilled with that name.
        await type_document(pilot, "\\fn monthly")
        assert app.palette.view.labels == ["monthly"]  # the one create offer
        await pilot.press("enter")
        await pilot.pause()
        screen = app.screen
        assert screen.query_one("#fn-name", Input).value == "monthly"
        assert screen.query_one("#fn-body", TextArea).text == ""  # blank body, new function
        assert str(screen.query_one("#fn-status", Static).content) == "new function"

    run_app(scenario)


def test_a_saved_function_completes_as_a_call_from_the_document():
    """In `\\from`, a saved function is offered as `name()` with the caret inside."""

    async def scenario(app, pilot):
        app.session.save_fn("hot", "min_amount", FN_BODY, "")
        app.editor.load_text("")
        await pilot.pause()
        await type_document(pilot, "\\from hot")
        # Accepting inserts the call and parks the caret between the parens, so
        # the argument is typed next to the cursor — the whole point of the row.
        await pilot.press("enter")
        await pilot.pause()
        assert app.editor.text == "\\from hot()"
        assert app.editor.cursor_location == (0, len("\\from hot("))
        # A call to a no-argument function needs no argument at all: it runs as-is.
        app.session.save_fn("all_events", "", "\\from events\n\\select user_id", "")
        app.editor.load_text("\\from all_events()\n\\select *")
        await pilot.press("ctrl+enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert text_of(app, "#results-error") == ""
        assert results_table(app).row_count == 100

    run_app(scenario)


def test_a_function_is_deleted_from_the_library():
    """Deleting removes it from the store and the list; the editor keeps its text."""

    async def scenario(app, pilot):
        app.session.save_fn("hot", "min_amount", FN_BODY, "")
        await type_document(pilot, "\\fn hot")
        await pilot.press("enter")
        await pilot.pause()
        screen = app.screen
        await pilot.click("#fn-delete")
        await pilot.pause()
        assert "hot" not in app.session.fns
        assert list(app.session.fns) == []
        # the fields are now a blank new-function form (the name field clears)
        assert screen.query_one("#fn-name", Input).value == ""
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert not isinstance(app.screen, FnScreen)

    run_app(scenario)


def test_fn_target_drives_completion_save_and_preview_without_changing_workspace():
    """A foreign target never leaks its schema or dialect into the workspace."""

    async def scenario(app, pilot):
        app.select_source("mysql")
        await pilot.pause()
        app.select_dialect("postgres")
        await pilot.pause()
        workspace_doc = app.editor.text
        app.action_fn()
        await pilot.pause()
        screen = app.screen
        source = screen.query_one("#fn-source", Select)
        body = screen.query_one("#fn-body", TextArea)
        palette = screen.query_one("#fn-palette")
        assert source.value == "mysql"

        body.focus()
        await type_document(pilot, "\\from ")
        assert "products" in palette.view.labels
        assert "events" not in palette.view.labels
        source.value = "demo"
        await pilot.pause()
        assert "events" in palette.view.labels
        assert "products" not in palette.view.labels
        assert body.text == "\\from "

        await fill_fn(screen, name="target_events", params="", body="\\from events\n\\select user_id\n\\limit 2")
        await pilot.press("ctrl+r")
        await pilot.pause()
        grid = screen.query_one("#fn-grid", DataTable)
        assert grid.row_count == 2
        assert "target_events" in app.session.fns
        assert app.session.history == []
        assert app.session.active_id == "mysql"
        assert app.session.dialect == "postgres"
        assert [table.name for table in schema_tables()] == list(app.session.source.datasets)

        # Validation and execution errors must restore the same workspace too.
        source.value = "mysql"
        await pilot.pause()
        assert grid.row_count == 0
        await pilot.click("#fn-save")
        await pilot.pause()
        assert screen.query_one("#fn-status", Static).has_class("error")
        source.value = "demo"
        await pilot.pause()
        await fill_fn(screen, name="target_events", params="limit", body="\\from events\n\\where amount > @limit\n\\select user_id")
        await pilot.press("ctrl+r")  # no argument supplied
        await pilot.pause()
        assert screen.query_one("#fn-status", Static).has_class("error")
        assert app.session.active_id == "mysql"
        assert app.session.dialect == "postgres"
        assert [table.name for table in schema_tables()] == list(app.session.source.datasets)
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert not isinstance(app.screen, FnScreen)
        assert app.editor.text == workspace_doc

    run_app(scenario)


def test_fn_body_completion_accepts_keys_and_clicks_without_editing_workspace():
    """Body completion owns its keys, while Escape/Tab still leave the form usable."""

    async def scenario(app, pilot):
        workspace_doc = app.editor.text
        app.action_fn()
        await pilot.pause()
        screen = app.screen
        body = screen.query_one("#fn-body", TextArea)
        palette = screen.query_one("#fn-palette")
        body.focus()
        await pilot.press("escape")
        assert app.screen is screen
        assert not palette.is_open  # An empty body has nothing to offer, not a reason to leave.
        await type_document(pilot, "\\fro")
        await pilot.press("tab")
        assert body.text == "\\from "
        await type_document(pilot, "ev")
        await pilot.press("enter")
        assert body.text == "\\from events "
        await pilot.press("enter")
        await type_document(pilot, "\\sel")
        await pilot.press("tab")
        await type_document(pilot, "us")
        assert palette.view.labels == ["user_id"]
        await pilot.click("#fn-palette", offset=(3, 1))
        await pilot.pause()
        assert body.text == "\\from events \n\\select user_id "
        assert app.focused is body
        await pilot.press("enter")
        await type_document(pilot, "\\")
        assert all(not entry.action for entry in palette.view.entries)
        await pilot.press("escape")
        assert app.screen is screen
        assert not palette.is_open
        await pilot.press("escape")
        assert app.screen is screen
        assert palette.is_open
        await pilot.press("escape")
        assert not palette.is_open
        await pilot.press("tab")
        assert app.focused is screen.query_one("#fn-args", Input)
        await pilot.press("escape")
        assert app.screen is screen  # Escape in a form field never closes the library.
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert not isinstance(app.screen, FnScreen)
        assert app.editor.text == workspace_doc

    run_app(scenario)


def test_fn_parameters_accept_bare_prefix_and_nested_tokens_then_preview():
    """Parameter accepts keep punctuation intact and produce an executable body."""

    async def scenario(app, pilot):
        app.action_fn()
        await pilot.pause()
        screen = app.screen
        await fill_fn(screen, name="threshold", params="min_amount", body="\\from events\n\\where amount > ", args="0")
        body = screen.query_one("#fn-body", TextArea)
        palette = screen.query_one("#fn-palette")
        body.focus()
        body.cursor_location = (1, len(body.document[1]))
        await type_document(pilot, "@")
        assert palette.view.labels == ["@min_amount"]
        await pilot.press("tab")
        assert body.text.endswith("amount > @min_amount")
        await pilot.press("enter")  # Accepting the exact name is a no-op, so Enter is a newline.
        assert body.text.endswith("@min_amount\n")

        body.load_text("\\from events\n\\where amount > ")
        body.cursor_location = (1, len(body.document[1]))
        await type_document(pilot, "@min")
        await pilot.press("enter")
        assert body.text == "\\from events\n\\where amount > @min_amount"
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert screen.query_one("#fn-grid", DataTable).row_count == 100
        assert not screen.query_one("#fn-status", Static).has_class("error")

        # Completing inside a stale token replaces its suffix, not its enclosing calls.
        nested = '\\select concat("a,b", upper(@min_stale), "tail") as label'
        body.load_text("\\from events\n" + nested)
        body.cursor_location = (1, nested.index("@min") + len("@min"))
        body.focus()
        await pilot.pause()
        assert "@min_amount" in palette.view.labels
        await pilot.click("#fn-palette", offset=(3, 1))
        await pilot.pause()
        assert body.text == "\\from events\n" + nested.replace("@min_stale", "@min_amount")
        assert app.focused is body

    run_app(scenario)


def test_fn_parameter_offers_follow_unsaved_declarations_and_function_switches():
    """Only the current form's valid declarations are offered, even before saving."""

    async def scenario(app, pilot):
        app.session.save_fn("first", "old_name", "\\from events\n\\where amount > @old_name", "")
        app.session.save_fn("second", "other_name", "\\from events\n\\where amount > @other_name", "")
        app.action_fn()
        await pilot.pause()
        screen = app.screen
        listing = screen.query_one("#fn-list", OptionList)
        listing.highlighted = 0
        listing.focus()
        await pilot.press("enter")
        body = screen.query_one("#fn-body", TextArea)
        palette = screen.query_one("#fn-palette")
        params = screen.query_one("#fn-params", Input)
        body.load_text("\\from events\n\\where amount > ")
        body.cursor_location = (1, len(body.document[1]))
        body.focus()
        await type_document(pilot, "@")
        assert palette.view.labels == ["@old_name"]

        params.value = "admin_limit, min_amount, min_amount, @bad, 3bad, , two words"
        await pilot.pause()
        assert palette.view.labels == ["@admin_limit", "@min_amount"]
        await type_document(pilot, "min")
        assert palette.view.labels == ["@min_amount", "@admin_limit"]
        assert app.session.fns["first"].params == ["old_name"]
        params.value = ""
        await pilot.pause()
        assert not palette.is_open
        params.value = "minimum"
        await pilot.pause()
        assert palette.view.labels == ["@minimum"]

        listing.highlighted = 1
        listing.focus()
        await pilot.press("enter")
        body.load_text("\\from events\n\\where amount > ")
        body.cursor_location = (1, len(body.document[1]))
        body.focus()
        await type_document(pilot, "@")
        assert palette.view.labels == ["@other_name"]
        listing.highlighted = 2  # A new function must not inherit the last signature.
        listing.focus()
        await pilot.press("enter")
        body.focus()
        await type_document(pilot, "@")
        assert not palette.is_open

    run_app(scenario)


def test_fn_parameter_completion_respects_quotes_dismissal_and_workspace_scope():
    """Quoted/email @ signs aren't placeholders, and neither dismissal leaks."""

    async def scenario(app, pilot):
        app.action_fn()
        await pilot.pause()
        screen = app.screen
        screen.query_one("#fn-params", Input).value = "minimum"
        body = screen.query_one("#fn-body", TextArea)
        palette = screen.query_one("#fn-palette")
        body.focus()
        for prefix in ('\\select concat("quoted, ', "\\where event_type = '", "\\where event_type = user"):
            body.load_text(prefix)
            body.cursor_location = (0, len(prefix))
            await type_document(pilot, "@min")
            assert palette.view is None or "@minimum" not in palette.view.labels

        body.load_text("\\from events\n\\where amount > ")
        body.cursor_location = (1, len(body.document[1]))
        await type_document(pilot, "@min")
        assert "@minimum" in palette.view.labels
        await pilot.press("escape")
        await type_document(pilot, "i")
        assert not palette.is_open
        await pilot.press("escape")
        assert "@minimum" in palette.view.labels
        await pilot.press("escape")
        await type_document(pilot, " @")
        assert "@minimum" in palette.view.labels

        app.session.intellisense = False
        await type_document(pilot, " @")
        await pilot.press("escape")
        assert not palette.is_open
        await pilot.press("ctrl+c")
        await pilot.pause()
        app.session.intellisense = True
        app.editor.load_text("\\from events\n\\where amount > ")
        app.editor.cursor_location = (1, len(app.editor.document[1]))
        await type_document(pilot, "@min")
        assert app.palette.view is None or "@minimum" not in app.palette.view.labels

    run_app(scenario)


def test_cancel_pending_d1_connection_never_registers_or_remembers(monkeypatch):
    """Escape stays responsive while connecting; a late result is discarded."""
    from tests.test_d1_sources import live_source

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    async def scenario(app, pilot):
        def delayed_source(*args, **kwargs):
            started.set()
            try:
                if not release.wait(5):
                    raise RuntimeError("connection was not cancelled promptly")
                source, _ = live_source()
                return source
            finally:
                finished.set()

        monkeypatch.setattr(app.session, "build_live_source", delayed_source)
        await pilot.press("ctrl+o")
        modal = app.screen
        modal.query_one("#account-id", Input).value = "account"
        modal.query_one("#database", Input).value = "orders-db"
        modal.query_one("#api-token", Input).value = "private-token"
        await pilot.pause()
        modal.query_one("#add", Button).scroll_visible(immediate=True)
        await pilot.pause()
        try:
            await pilot.click("#add")
            assert await asyncio.to_thread(started.wait, 2)
            await pilot.press("escape")
            assert not isinstance(app.screen, AddSourceModal)
            release.set()
            assert await asyncio.to_thread(finished.wait, 2)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.session.active_id == "demo"
            assert "orders-db" not in app.session.sources
            assert app.session.d1_profiles == []
            assert not app.session.storage_path.exists()
        finally:
            release.set()

    run_app(scenario)
