"""Deterministic data generator for anyQL's engine.

Pure arithmetic — no randomness anywhere, so re-running reproduces byte-identical
Parquet and SQLite files. Run from the repo root:

    python -m anyql.engine.make_data       # or: python anyql/engine/make_data.py

Outputs:
- anyql/engine/data/events.parquet, anyql/engine/data/users.parquet
- anyql/engine/data/<mock-id>/*.parquet (mock datasource mirrors)
- anyql/engine/d1/d1.sqlite (invented stations and temperature readings)
- tests/fixtures/expected_schema.json, expected_rows.json,
  expected_mock_rows.json
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import ibis
import pyarrow as pa
import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"

EVENT_TYPES = ["click", "view", "purchase", "signup"]
REGIONS = ["eu", "us", "apac"]

# Payload whose result is pinned as the execute regression fixture.
FIRST10_PAYLOAD = {
    "dataset": "events",
    "select": [],
    "where": None,
    "groupBy": [],
    "orderBy": [{"target": "timestamp", "direction": "asc"}],
    "limit": 10,
}

# Payload run against the mock Postgres mirror; its result is pinned as the
# mock-execute regression fixture.
MOCK_TOP_REGIONS_PAYLOAD = {
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


def events_table() -> pa.Table:
    timestamps = [dt.datetime(2024, 1, 1) + dt.timedelta(minutes=37 * i) for i in range(100)]
    user_ids = [1 + (i * 7) % 25 for i in range(100)]
    event_types = [EVENT_TYPES[i % 4] for i in range(100)]
    amounts = [round((i % 13) * 1.5 + 0.99, 2) for i in range(100)]
    paths = [f"/p/{i % 9}" for i in range(100)]
    return pa.table(
        {
            "timestamp": pa.array(timestamps, type=pa.timestamp("us")),
            "user_id": pa.array(user_ids, type=pa.int64()),
            "event_type": pa.array(event_types, type=pa.string()),
            "amount": pa.array(amounts, type=pa.float64()),
            "path": pa.array(paths, type=pa.string()),
        }
    )


def users_table() -> pa.Table:
    user_ids = list(range(1, 26))
    regions = [REGIONS[k % 3] for k in range(25)]
    joined = [dt.datetime(2023, 6, 1) + dt.timedelta(days=11 * k) for k in range(25)]
    scores = [(k * 13) % 100 for k in range(25)]
    return pa.table(
        {
            "user_id": pa.array(user_ids, type=pa.int64()),
            "region": pa.array(regions, type=pa.string()),
            "joined": pa.array(joined, type=pa.timestamp("us")),
            "score": pa.array(scores, type=pa.int64()),
        }
    )


def write_d1_snapshot(path: Path | None = None) -> Path:
    """Replace the snapshot with a fresh database, never opening the old file."""
    path = HERE / "d1" / "d1.sqlite" if path is None else Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".synthetic-d1-", dir=path.parent) as directory:
        fresh = Path(directory) / "d1.sqlite"
        con = sqlite3.connect(fresh)
        try:
            with con:
                con.execute("CREATE TABLE stations (station_id INTEGER PRIMARY KEY, label TEXT NOT NULL)")
                con.execute(
                    "CREATE TABLE readings (reading_id INTEGER PRIMARY KEY, "
                    "station_id INTEGER NOT NULL REFERENCES stations(station_id), "
                    "temperature_c REAL NOT NULL)"
                )
                con.executemany(
                    "INSERT INTO stations VALUES (?, ?)",
                    [(1, "Demo North"), (2, "Demo Central"), (3, "Demo South")],
                )
                con.executemany(
                    "INSERT INTO readings VALUES (?, ?, ?)",
                    ((i + 1, i % 3 + 1, 18.0 + i * 0.5) for i in range(12)),
                )
        finally:
            con.close()
        fresh.replace(path)
    return path


def main() -> None:
    try:
        from anyql.engine import datasources
        from anyql.engine.execute import execute
    except ImportError:  # direct-script execution: make repo root importable
        sys.path.insert(0, str(REPO_ROOT))
        from anyql.engine import datasources
        from anyql.engine.execute import execute

    data_dir = datasources.DATA_DIR
    data_dir.mkdir(parents=True, exist_ok=True)
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)

    events = events_table()
    users = users_table()
    pq.write_table(events, data_dir / "events.parquet")
    pq.write_table(users, data_dir / "users.parquet")

    # Fixtures are computed with the real execute pipeline and the engine's own
    # dtype labels, so each one pins exactly what the engine hands the UI.
    con = ibis.duckdb.connect()
    con.create_table("events", con.read_parquet(str(data_dir / "events.parquet")))
    con.create_table("users", con.read_parquet(str(data_dir / "users.parquet")))
    result = execute(con, FIRST10_PAYLOAD)

    rows_fixture = {"columns": result["columns"], "rows": result["rows"]}
    schema_fixture = {
        "dataset": "events",
        "columns": [
            {"name": name, "type": datasources.type_name(dtype)}
            for name, dtype in con.table("events").schema().items()
        ],
    }

    (FIXTURES_DIR / "expected_schema.json").write_text(
        json.dumps(schema_fixture, indent=2) + "\n", encoding="utf-8"
    )
    (FIXTURES_DIR / "expected_rows.json").write_text(
        json.dumps(rows_fixture, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {data_dir / 'events.parquet'} ({events.num_rows} rows)")
    print(f"wrote {data_dir / 'users.parquet'} ({users.num_rows} rows)")
    print(f"wrote {FIXTURES_DIR / 'expected_schema.json'}")
    print(f"wrote {FIXTURES_DIR / 'expected_rows.json'}")

    # Mock datasource mirrors: one Parquet tree per connection under data/<id>/,
    # plus a mock-execute fixture locking the mock -> execute contract.
    datasources.write_mock_data(data_dir)
    for spec in datasources.MOCKS:
        for table in spec.tables:
            path = data_dir / spec.id / f"{table.name}.parquet"
            print(f"wrote {path}")
    mock = ibis.duckdb.connect()
    mock.create_table(
        "orders", mock.read_parquet(str(data_dir / "postgres" / "orders.parquet"))
    )
    mock.create_table(
        "customers", mock.read_parquet(str(data_dir / "postgres" / "customers.parquet"))
    )
    mock_result = execute(mock, MOCK_TOP_REGIONS_PAYLOAD)
    (FIXTURES_DIR / "expected_mock_rows.json").write_text(
        json.dumps({"columns": mock_result["columns"], "rows": mock_result["rows"]}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {FIXTURES_DIR / 'expected_mock_rows.json'}")

    print(f"wrote {write_d1_snapshot()} (synthetic stations and readings)")


if __name__ == "__main__":
    # Works as `python anyql/engine/make_data.py` and `python -m anyql.engine.make_data`.
    main()
