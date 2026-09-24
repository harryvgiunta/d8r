"""Datasource registry + dialect compile surface: the mock-connection contract.

Mocks execute deterministic data through an in-process DuckDB mirror; SQL
compiles to any advertised dialect without any real database connection.
"""

from __future__ import annotations

import json

import pytest

from anyql.engine import (
    DIALECTS,
    PayloadError,
    capabilities_for,
    column_values,
    datasources,
    dialect_for,
    execute,
    expression,
    load,
)
from tests.conftest import DATA_DIR, FIXTURES_DIR

PG_JOIN_PAYLOAD = {
    "dataset": "orders",
    "joins": [{"dataset": "customers", "left": "customer_id", "right": "customer_id"}],
    "select": [
        {"column": "region", "aggregate": None, "alias": None},
        {"column": None, "aggregate": {"fn": "sum", "arg": "amount"}, "alias": None},
    ],
    "where": None,
    "groupBy": ["region"],
    "orderBy": [{"target": "amount_sum", "direction": "desc"}],
    "limit": 5,
}


def test_registry_lists_demo_first_then_mocks(sources):
    assert list(sources) == ["demo", "postgres", "mysql", "snowflake", "bigquery"]
    demo = sources["demo"]
    assert demo.kind == "demo"
    assert demo.dialect == "duckdb"
    assert set(demo.datasets) == {"events", "users"}
    pg = sources["postgres"]
    assert pg.kind == "mock"
    assert pg.dialect == "postgres"
    assert set(pg.datasets) == {"orders", "customers"}
    assert all(e["doc"] for s in sources.values() for e in s.datasets.values())


def test_datasets_are_scoped_to_their_datasource(sources):
    assert set(sources["postgres"].datasets) == {"orders", "customers"}
    assert set(sources["demo"].datasets) == {"events", "users"}


def test_load_defaults_to_the_bundled_tree(monkeypatch, tmp_path):
    """The registry finds its own data: no path argument, no working directory."""
    assert datasources.DATA_DIR == DATA_DIR
    monkeypatch.chdir(tmp_path)
    default, explicit = load(), load(DATA_DIR)
    assert list(default) == list(explicit)
    assert {n: e["rows"] for n, e in default["demo"].datasets.items()} == {
        n: e["rows"] for n, e in explicit["demo"].datasets.items()
    }


def test_mock_schema_has_expected_columns(sources):
    columns = [
        {"name": name, "type": datasources.type_name(dtype)}
        for name, dtype in sources["postgres"].datasets["orders"]["table"].schema().items()
    ]
    assert {"name": "amount", "type": "float64"} in columns
    assert {"name": "placed_at", "type": "timestamp"} in columns


def test_mock_column_values(sources):
    assert column_values(sources["postgres"], "orders", "status") == ["cancelled", "new", "paid", "shipped"]


def test_value_lookup_rejects_unknown_dataset_and_column(sources):
    with pytest.raises(PayloadError, match="unknown dataset"):
        column_values(sources["postgres"], "bogus", "status")
    with pytest.raises(PayloadError, match="unknown column"):
        column_values(sources["postgres"], "orders", "bogus")


def test_mock_capabilities_label_honest(sources):
    caps = capabilities_for(sources["snowflake"])
    assert caps["backend"] == "snowflake (mock)"
    assert caps["aggregates"]  # same ibis-translatable surface as demo


def test_dialects_lists_all_with_compile_flags():
    by_name = {d["name"]: d for d in DIALECTS}
    assert {"duckdb", "postgres", "mysql", "snowflake", "bigquery", "trino"} <= by_name.keys()
    assert all(d["compiles"] for name, d in by_name.items() if name != "pyspark")
    assert by_name["pyspark"]["compiles"] is False  # known but uncompilable offline


def test_compile_targets_any_dialect_without_executing(sources):
    expr = expression.build(sources["postgres"].con, PG_JOIN_PAYLOAD)
    sql = expression.compile_sql(expr, dialect="snowflake")
    assert 'SUM("t' in sql  # Snowflake double-quoting
    assert "amount_sum" in sql
    assert "`" not in sql  # not the mock's own mysql-style dialect


def test_compile_uses_datasource_default_dialect(sources):
    source = sources["mysql"]
    assert dialect_for(source) == "mysql"
    sql = expression.compile_sql(
        expression.build(source.con, {"dataset": "products", "limit": 3}), dialect=dialect_for(source)
    )
    assert "`products`" in sql  # MySQL backticks


def test_dialect_for_rejects_unknown_name(sources):
    with pytest.raises(PayloadError, match="unknown dialect"):
        dialect_for(sources["postgres"], "oracle_free_edition")


def test_dialect_for_rejects_uncompilable_dialect(sources):
    with pytest.raises(PayloadError, match="does not compile"):
        dialect_for(sources["postgres"], "pyspark")


def test_build_rejects_unknown_column(sources):
    with pytest.raises(PayloadError, match="bogus"):
        expression.build(sources["postgres"].con, {"dataset": "orders", "select": [{"column": "bogus"}]})


def test_execute_on_mock_matches_fixture(sources):
    source = sources["postgres"]
    result = execute(source.con, PG_JOIN_PAYLOAD, dialect=source.dialect)
    fixture = json.loads((FIXTURES_DIR / "expected_mock_rows.json").read_text(encoding="utf-8"))
    assert result["columns"] == fixture["columns"]
    assert result["rows"] == fixture["rows"]
    assert result["dialect"] == "postgres"


def test_execute_cross_dialect_sql_same_rows(sources):
    """Dialect changes the SQL text only; rows come from the datasource."""
    source = sources["postgres"]
    duck = execute(source.con, PG_JOIN_PAYLOAD, dialect="duckdb")
    mssql = execute(source.con, PG_JOIN_PAYLOAD, dialect="mssql")
    assert duck["rows"] == mssql["rows"]
    assert "TOP 5" in mssql["sql"]  # SQL Server caps with TOP, not LIMIT
    assert "LIMIT 5" in duck["sql"]


def test_every_advertised_dialect_compiles_or_says_it_cannot(sources):
    """The whole advertised compile surface is truthful, offline."""
    source = sources["postgres"]
    payload = {"dataset": "orders", "limit": 3}
    for spec in DIALECTS:
        if not spec["compiles"]:
            with pytest.raises(PayloadError, match="does not compile"):
                dialect_for(source, spec["name"])
            continue
        sql = expression.compile_sql(expression.build(source.con, payload), dialect=dialect_for(source, spec["name"]))
        assert "select" in sql.lower(), spec["name"]


def test_mock_data_is_deterministic():
    """Regenerating mirrors from the builders yields byte-identical files."""
    import pyarrow.parquet as pq

    for spec in datasources.MOCKS:
        for table in spec.tables:
            path = DATA_DIR / spec.id / f"{table.name}.parquet"
            assert pq.read_table(path).equals(table.build()), path
