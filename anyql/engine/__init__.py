"""anyQL's Python engine: the datasource registry plus payload execution.

Three seams, no UI:

* `datasources` — the registry (`load`), what it advertises (`capabilities_for`,
  `DIALECTS`), and the two ways a real database joins it: `add_sqlite_source`
  for a local D1/SQLite snapshot, `add_d1_live_source` for a live Cloudflare D1
  reached over its API.
* `expression` — payload -> ibis expression (`build`) and SQL rendering for any
  advertised dialect (`compile_sql`).
* `execute` — running a payload (`execute`, `execute_remote`) and the rows that
  come back in the shape a result pane renders.

Everything a caller needs is re-exported here. Two faults are the user's to
read and nothing else: `PayloadError` for a bad payload or dialect choice, and
`D1Error` for a D1-side failure. The name `execute` below is the *function*;
its module is importable as `from anyql.engine.execute import execute_remote`.
"""

from __future__ import annotations

from .d1api import CloudflareD1, D1Error, schema_connection
from .datasources import (
    CAPABILITIES,
    CATALOG,
    DATA_DIR,
    DEMO_ID,
    DIALECT_BY_NAME,
    DIALECTS,
    MOCKS,
    DataSource,
    add_d1_live_source,
    add_sqlite_source,
    capabilities_for,
    column_values,
    dialect_for,
    ensure_mock_data,
    load,
    type_name,
    write_mock_data,
)
from .execute import execute, execute_remote
from .expression import PayloadError, build, compile_sql

__all__ = [
    "CAPABILITIES",
    "CATALOG",
    "CloudflareD1",
    "D1Error",
    "DATA_DIR",
    "DEMO_ID",
    "DIALECT_BY_NAME",
    "DIALECTS",
    "MOCKS",
    "DataSource",
    "PayloadError",
    "add_d1_live_source",
    "add_sqlite_source",
    "build",
    "capabilities_for",
    "column_values",
    "compile_sql",
    "dialect_for",
    "ensure_mock_data",
    "execute",
    "execute_remote",
    "load",
    "schema_connection",
    "type_name",
    "write_mock_data",
]
