"""Shared fixtures: one engine registry bound to the real bundled data tree.

`DATA_DIR` is the engine's own Parquet tree (resolved from this file, not the
working directory), so every test runs against the data anyQL actually ships.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from anyql.engine import DataSource, datasources
from anyql.query import set_schema_state, set_fns

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "anyql" / "engine" / "data"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

FIRST10_PAYLOAD = {
    "dataset": "events",
    "select": [],
    "where": None,
    "groupBy": [],
    "orderBy": [{"target": "timestamp", "direction": "asc"}],
    "limit": 10,
}

JOIN_REVENUE_PAYLOAD = {
    "dataset": "events",
    "joins": [{"dataset": "users", "left": "user_id", "right": "user_id"}],
    "select": [
        {"column": "region", "aggregate": None, "alias": None},
        {"column": None, "aggregate": {"fn": "sum", "arg": "amount"}, "alias": "revenue"},
    ],
    "where": None,
    "groupBy": ["region"],
    "orderBy": [{"target": "revenue", "direction": "desc"}],
    "limit": 5,
}

ALIAS_REVENUE_PAYLOAD = {
    "dataset": "events",
    "alias": "e",
    "joins": [{"dataset": "users", "alias": "u", "left": "user_id", "right": "user_id"}],
    "select": [
        {"column": "u.region", "aggregate": None, "alias": None},
        {"column": None, "aggregate": {"fn": "sum", "arg": "e.amount"}, "alias": None},
    ],
    "where": None,
    "groupBy": ["u.region"],
    "orderBy": [{"target": "amount_sum", "direction": "desc"}],
    "limit": 5,
}


@pytest.fixture(autouse=True)
def _isolated_memory(tmp_path, monkeypatch):
    """No test reads or writes the user's durable anyQL memory."""
    monkeypatch.setenv("ANYQL_DATA_DIR", str(tmp_path / "anyql-data"))


@pytest.fixture(autouse=True)
def _clean_schema_registry():
    """Every test starts (and ends) with the language's registries empty.

    Both are module-level seams, so a file that installs a schema or a saved
    function — the TUI's sessions, the parser's loaded regime — would otherwise
    leak it into whatever runs next. A test that needs one installs it itself.
    """
    set_schema_state([])
    set_fns([])
    yield
    set_schema_state([])
    set_fns([])


@pytest.fixture(scope="session")
def sources() -> dict[str, DataSource]:
    """The registry the UI holds: `demo` first, then every mock, in order."""
    return datasources.load(DATA_DIR)


@pytest.fixture(scope="session")
def con(sources: dict[str, DataSource]):
    """The demo connection every payload test builds against."""
    return sources[datasources.DEMO_ID].con
