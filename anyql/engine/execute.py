"""Execute an ExecutePayload against a registered Ibis DuckDB connection.

Kept separate from the UI so `anyql.engine.make_data` can compute fixtures with
the exact same serialization the result pane renders.
"""

from __future__ import annotations

import datetime as dt
import math
import time
from decimal import Decimal

import numpy as np
import pandas as pd

from . import expression, tx


def _to_py(value):
    """Convert a DB/API result scalar into a JSON-safe Python value."""
    if value is None or value is pd.NaT:
        return None
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, (dt.datetime, dt.date, dt.time)):  # covers pd.Timestamp
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    return value


def execute(con, payload: dict, dialect: str | None = None, tables: dict | None = None) -> dict:
    """Build, compile (SQL without executing), then run the payload.

    Returns `{columns, rows, sql, dialect, ms}`; `ms` is wall-clock execute
    time, `sql` renders for `dialect` (None = the connection's own backend).
    `tables` are the source's datasets, already resolved to expressions — see
    `expression.get_table` for why a caller that owns the registry passes them.
    Raises `expression.PayloadError` for user errors.
    """
    expr = expression.build(con, payload, tables=tables)
    sql = expression.compile_sql(expr, dialect=dialect)
    started = time.perf_counter()
    frame = expr.execute()
    return _result(frame, sql, dialect, started)


def materialize(
    con, payload: dict, name: str, dialect: str | None = None, tables: dict | None = None
) -> dict:
    """Run the payload once and keep its result as a temp table named `name`.

    `\\temp` is SQL's `CREATE TEMP TABLE … AS`: the document still answers with
    the rows it asked for, and the same rows stay behind on the connection for
    later documents to name. The rows come *from the table*, so what the pane
    shows and what a later document reads can never disagree.
    """
    expr = expression.build(con, payload, tables=tables)
    sql = expression.compile_sql(expr, dialect=dialect)
    started = time.perf_counter()
    tx.create_temp(con, name, expr)
    frame = tx.temp_handle(con, name, expr.schema()).execute()
    return _result(frame, sql, dialect, started)


def _result(frame, sql: str, dialect: str | None, started: float) -> dict:
    """The run's public shape: columns, JSON-safe rows, the SQL, and the clock."""
    ms = (time.perf_counter() - started) * 1000.0
    columns = [str(name) for name in frame.columns]
    rows = [[_to_py(value) for value in row] for row in frame.itertuples(index=False, name=None)]
    return {"columns": columns, "rows": rows, "sql": sql, "dialect": dialect, "ms": ms}


def execute_remote(
    d1, con, payload: dict, dialect: str, tables: dict | None = None
) -> dict:
    """Run a payload against a live Cloudflare D1 database over its HTTP API.

    Same build path as `execute` — the payload builds against the source's
    unbound schema tables and every ibis error surfaces identically — but the
    SQL that actually runs is *always* SQLite (that is the SQL D1's engine
    speaks). `dialect` only chooses what the pane *shows*: the result's `sql`
    renders for it, so the compile pane stays truthful about what another
    database would receive while execution stays on the real D1.
    """
    expr = expression.build(con, payload, tables=tables)
    run_sql = expression.compile_sql(expr, dialect="sqlite")
    shown = expression.compile_sql(expr, dialect=dialect)
    started = time.perf_counter()
    columns, rows = d1.raw(run_sql)
    ms = (time.perf_counter() - started) * 1000.0
    return {"columns": columns, "rows": [[_to_py(value) for value in row] for row in rows], "sql": shown, "dialect": dialect, "ms": ms}
