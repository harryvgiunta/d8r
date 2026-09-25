"""D8R's Python engine: the datasource registry plus payload execution.

Three seams, no UI:

* `datasources` — the registry (`load`), what it advertises (`capabilities_for`,
  `DIALECTS`), and the two ways a real database joins it: `add_sqlite_source`
  for a local D1/SQLite snapshot, `add_d1_live_source` for a live Cloudflare D1
  reached over its API.
* `expression` — payload -> ibis expression (`build`) and SQL rendering for any
  advertised dialect (`compile_sql`).
* `execute` — running a payload (`execute`, `execute_remote`, `materialize`)
  and the rows that come back in the shape a result pane renders.
* `tx` — the statements a payload cannot carry: transactions, savepoints,
  and the temp tables a document keeps (`d8r.engine.tx`).

Everything a caller needs is re-exported here. Two faults are the user's to
read and nothing else: `PayloadError` for a bad payload or dialect choice, and
`D1Error` for a D1-side failure. The name `execute` below is the *function*;
its module is importable as `from d8r.engine.execute import execute_remote`.
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
from .execute import execute, execute_remote, materialize
from .expression import PayloadError, build, compile_sql
from .tx import (
    begin,
    commit,
    create_temp,
    drop_temp,
    engine_of,
    release,
    rollback,
    rollback_to,
    savepoint,
    temp_handle,
)

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
    "begin",
    "build",
    "capabilities_for",
    "column_values",
    "commit",
    "compile_sql",
    "create_temp",
    "dialect_for",
    "drop_temp",
    "engine_of",
    "ensure_mock_data",
    "execute",
    "execute_remote",
    "load",
    "materialize",
    "release",
    "rollback",
    "rollback_to",
    "savepoint",
    "schema_connection",
    "temp_handle",
    "type_name",
    "write_mock_data",
]
