"""The schema surface: engine dtype labels against the checked-in fixture."""

from __future__ import annotations

import json

import pytest

from d8r.engine import PayloadError, datasources, expression
from tests.conftest import FIXTURES_DIR


def load(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def columns_of(table) -> list[dict]:
    """The column list a schema pane renders, in table order."""
    return [{"name": name, "type": datasources.type_name(dtype)} for name, dtype in table.schema().items()]


def test_events_schema_matches_fixture(con):
    assert {"dataset": "events", "columns": columns_of(con.table("events"))} == load("expected_schema.json")


def test_events_schema_columns_and_types(con):
    columns = columns_of(con.table("events"))
    assert [c["name"] for c in columns] == ["timestamp", "user_id", "event_type", "amount", "path"]
    assert [c["type"] for c in columns] == ["timestamp", "int64", "string", "float64", "string"]


def test_users_schema(con):
    assert columns_of(con.table("users")) == [
        {"name": "user_id", "type": "int64"},
        {"name": "region", "type": "string"},
        {"name": "joined", "type": "timestamp"},
        {"name": "score", "type": "int64"},
    ]


def test_unknown_dataset_has_no_schema(sources, con):
    assert "nope" not in sources[datasources.DEMO_ID].datasets
    with pytest.raises(PayloadError, match="unknown dataset"):
        expression.build(con, {"dataset": "nope"})
