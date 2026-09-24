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
def restore_schema_seam():
    """The schema registry is process-global: hand it back as it was found."""
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


def test_a_parser_error_shows_and_does_not_execute():
    """A settled bad line stops the run at the parser, with its message."""

    async def scenario(app, pilot):
        app.editor.load_text("\\nope\n\\from events")
        await pilot.press("ctrl+enter")

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
        assert [field.id for field in modal.query(Input)] == [
            "account-id",
            "database",
            "api-token",
            "snapshot-path",
            "display-name",
        ]
        assert modal.query_one("#api-token", Input).password is True
        assert [button.id for button in modal.query(Button)] == ["test", "add", "cancel"]

        modal.query_one("#account-id", Input).value = "00000000000000000000000000000000"
        modal.query_one("#api-token", Input).value = "secret-token"
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
            await pilot.click("#add")

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
        assert results_table(app).row_count == min(100, PREVIEW_ROW_CAP) == 100
        assert "preview capped" not in text_of(app, "#results-status")

        # A source with more rows than the buffer holds.
        app.session.register(add_sqlite_source("big", path, "Big snapshot"))
        app.query_one("#source-select", Select).set_options(app._source_options())
        app.query_one("#source-select", Select).value = "big"
        app.editor.load_text("\\from readings")
        await pilot.press("ctrl+enter")

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
        for command in ("from", "open", "join", "select", "where", "group", "order", "case", "limit", "with"):
            assert f"\\{command}" in labels
        assert [entry.label for entry in app.palette.view.entries if entry.action] == [
            "Run",
            "Compile",
            "Data source…",
            "History",
            "Results",
            "SQL",
            "Schema",
            "Settings",
        ]

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
        # Fields first, then the functions the live capabilities advertise.
        assert app.palette.view.labels == [
            "timestamp",
            "user_id",
            "event_type",
            "amount",
            "path",
            "sum",
            "avg",
            "count",
            "min",
            "max",
            "year",
            "month",
            "day",
            "quarter",
            "hour",
            "minute",
            "second",
            "rank",
            "dense_rank",
            "row_number",
        ]

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
        assert app.palette.view.labels == ["sum"]
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
        assert app.palette.view.labels == ["Results"]  # an action, not a command
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
        assert app.palette.view.labels == ["Results"]  # matching is case-insensitive
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
    """Select the open Settings row whose label or value is `match`."""
    screen = app.screen
    assert isinstance(screen, SettingsScreen)
    menu = screen.query_one("#settings-menu", OptionList)
    menu.highlighted = next(
        index for index, row in enumerate(screen.rows) if match in (row.label, row.value)
    )
    await pilot.press("enter")
    await pilot.pause()


def test_settings_lists_every_submenu_and_leaves_on_escape():
    """`\\settings` opens full screen, and escape leaves it from the root."""

    async def scenario(app, pilot):
        screen = await open_settings(app, pilot)
        assert screen.query_one("#settings").outer_size == app.size  # it takes the screen
        assert [row.label for row in screen.rows] == [
            "Show/Hide Menus",
            "Intellisense",
            "Keybindings",
            "Data source",
            "Dialect",
        ]
        assert screen.rows[1].detail == "on"

        await pick(pilot, app, "Intellisense")
        assert app.session.intellisense is False
        assert screen.rows[1].detail == "off"

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

        await pilot.press("escape")  # a submenu backs out to the root first
        assert [row.label for row in screen.rows][0] == "Show/Hide Menus"
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
        await pilot.press("escape")

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
        await pilot.press("escape")
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
