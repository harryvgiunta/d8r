"""Empirical function matrix: catalog functions x compile dialects x live backends.

Representative ibis call per function; compiled offline for every compilable
DIALECTS entry, and executed on each live backend (demo DuckDB, D1 snapshot
SQLite, live PostgreSQL when D8R_TEST_POSTGRES=1). Prints JSON.
"""
from __future__ import annotations

import json
import os

import ibis

from d8r.engine import datasources as ds
from d8r.engine.expression import compile_sql

STUB = {"s": "string", "i": "int64", "f": "float64", "t": "timestamp"}
TS_TEXT = "2024-01-01 00:00:00"


def calls() -> dict[str, object]:
    return {
        "string": lambda t: t.i.cast("string"),
        "coalesce": lambda t: t.s.coalesce("x"),
        "nullif": lambda t: t.s.nullif("x"),
        "concat": lambda t: t.s.concat("a"),
        "concat_ws": lambda t: ibis.literal("-").join([t.s, ibis.literal("a")]),
        "lower": lambda t: t.s.lower(),
        "upper": lambda t: t.s.upper(),
        "capitalize": lambda t: t.s.capitalize(),
        "length": lambda t: t.s.length(),
        "strip": lambda t: t.s.strip(),
        "lstrip": lambda t: t.s.lstrip(),
        "rstrip": lambda t: t.s.rstrip(),
        "substr": lambda t: t.s.substr(0, 2),
        "left": lambda t: t.s.left(2),
        "right": lambda t: t.s.right(2),
        "replace": lambda t: t.s.replace("a", "b"),
        "contains": lambda t: t.s.contains("a"),
        "startswith": lambda t: t.s.startswith("a"),
        "endswith": lambda t: t.s.endswith("a"),
        "repeat": lambda t: t.s.repeat(2),
        "reverse": lambda t: t.s.reverse(),
        "lpad": lambda t: t.s.lpad(5, "0"),
        "rpad": lambda t: t.s.rpad(5, "0"),
        "find": lambda t: t.s.find("a"),
        "translate": lambda t: t.s.translate("ab", "xy"),
        "levenshtein": lambda t: t.s.levenshtein("x"),
        "url_protocol": lambda t: t.s.protocol(),
        "url_host": lambda t: t.s.host(),
        "url_path": lambda t: t.s.path(),
        "url_query": lambda t: t.s.query(),
        "url_fragment": lambda t: t.s.fragment(),
        "as_date": lambda t: t.s.as_date("%Y-%m-%d"),
        "as_time": lambda t: t.s.as_time("%H:%M:%S"),
        "as_timestamp": lambda t: t.s.as_timestamp("%Y-%m-%d %H:%M:%S"),
        "convert_base": lambda t: t.s.convert_base(16, 10),
        "year": lambda t: t.t.year(),
        "month": lambda t: t.t.month(),
        "day": lambda t: t.t.day(),
        "quarter": lambda t: t.t.quarter(),
        "hour": lambda t: t.t.hour(),
        "minute": lambda t: t.t.minute(),
        "second": lambda t: t.t.second(),
        "sum": lambda t: t.f.sum(),
        "avg": lambda t: t.f.mean(),
        "count": lambda t: t.count(),
        "min": lambda t: t.f.min(),
        "max": lambda t: t.f.max(),
        "count_distinct": lambda t: t.s.nunique(),
        "median": lambda t: t.f.median(),
        "quantile": lambda t: t.f.quantile(0.5),
        "variance": lambda t: t.f.var(),
        "std": lambda t: t.f.std(),
        "approx_median": lambda t: t.f.approx_median(),
        "approx_quantile": lambda t: t.f.approx_quantile(0.5),
        "group_concat": lambda t: t.s.group_concat(","),
        "arbitrary": lambda t: t.s.arbitrary(),
        "first": lambda t: t.s.first(),
        "last": lambda t: t.s.last(),
        "rank": lambda t: ibis.rank().over(order_by=t.i) + 1,
        "dense_rank": lambda t: ibis.dense_rank().over(order_by=t.i) + 1,
        "row_number": lambda t: ibis.row_number().over(order_by=t.i) + 1,
    }


FAMILY = {}
for _fn in calls():
    FAMILY[_fn] = "string"
for _fn in ("year", "month", "day", "quarter", "hour", "minute", "second"):
    FAMILY[_fn] = "temporal"
for _fn in (
    "sum", "avg", "count", "min", "max", "count_distinct", "median", "quantile",
    "variance", "std", "approx_median", "approx_quantile", "group_concat",
    "arbitrary", "first", "last",
):
    FAMILY[_fn] = "aggregate"
for _fn in ("rank", "dense_rank", "row_number"):
    FAMILY[_fn] = "rank-window"
for _fn in ("string", "coalesce", "nullif"):
    FAMILY[_fn] = "cross-type"


def stub_on(con):
    return con.sql("SELECT '2024-01-02' AS s, 1 AS i, 1.5 AS f, "
                   f"CAST('{TS_TEXT}' AS TIMESTAMP) AS t", schema=STUB)


def probe():
    home = ibis.duckdb.connect()  # build host for offline compile probes
    results = {}
    for fn_name, make in calls().items():
        row = {"family": FAMILY[fn_name]}
        # compile probes: build once, render per dialect
        base = make(stub_on(home)).name("x")
        for spec in ds.DIALECTS:
            if not spec["compiles"]:
                continue
            try:
                compile_sql(base, dialect=spec["name"])
                row.setdefault("compile_ok", []).append(spec["name"])
            except Exception:
                row.setdefault("compile_no", []).append(spec["name"])
        # live DuckDB: the build host executes
        try:
            base.as_table().limit(1).execute()
            row["duckdb"] = "ok"
        except Exception as exc:
            row["duckdb"] = type(exc).__name__
        # live SQLite: D1 snapshot file via ibis sqlite backend
        try:
            sq = ibis.sqlite.connect("d8r/engine/d1/d1.sqlite")
            t = sq.sql("SELECT '2024-01-02' AS s, r.reading_id AS i, r.temperature_c AS f, "
                       f"CAST('{TS_TEXT}' AS TIMESTAMP) AS t "
                       "FROM readings r JOIN stations s USING (station_id)", schema=STUB)
            make(t).name("x").as_table().limit(1).execute()
            row["sqlite"] = "ok"
        except Exception as exc:
            row["sqlite"] = type(exc).__name__
        results[fn_name] = row
    if os.environ.get("D8R_TEST_POSTGRES") == "1":
        pg_specs = {}
        try:
            src = ds.add_postgres_source(
                "function-probe",
                host=os.environ.get("PGHOST", "127.0.0.1"),
                port=int(os.environ.get("PGPORT", "55433")),
                database=os.environ.get("PGDATABASE", "d8r"),
                user=os.environ.get("PGUSER", "d8r"),
                password=os.environ.get("PGPASSWORD", ""),
                schema=os.environ.get("PGSCHEMA", "public"),
                display="function-probe",
            )
            con = src.con
            for fn_name, make in calls().items():
                try:
                    t = con.sql(f"SELECT '2024-01-02' AS s, 1::int8 AS i, 1.5::float8 AS f, "
                                f"CAST('{TS_TEXT}' AS TIMESTAMP) AS t", schema=STUB)
                    make(t).name("x").as_table().limit(1).execute()
                    pg_specs[fn_name] = "ok"
                except Exception as exc:
                    pg_specs[fn_name] = type(exc).__name__
        except Exception as exc:
            pg_specs = {fn: f"setup:{type(exc).__name__}" for fn in calls()}
        for fn_name, row in results.items():
            row["postgres"] = pg_specs[fn_name]
    return results


if __name__ == "__main__":
    print(json.dumps(probe(), indent=1))
