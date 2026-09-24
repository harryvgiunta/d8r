"""Connection-level statements: transactions, savepoints, and temp tables.

The one thing a payload cannot express. Everywhere else this engine composes
ibis expressions and renders them with `ibis.to_sql`; a transaction is not an
expression — it is state on the connection the queries run on — so it is issued
through the backend's own handle instead.

Nothing here writes SQL by hand: a temp table is `CREATE TEMPORARY TABLE … AS`
around ibis' own rendering of the expression (`to_sql` for the connection's own
backend), and the only interpolated names are identifiers by grammar
(`[A-Za-z_][A-Za-z0-9_]*`). ibis' `create_table`/`drop_table` are deliberately
not used: on SQLite `create_table` opens a transaction of its own, which would
silently end the session's open one, and in this build's sqlglot their DROP
rendering loses the table name entirely (`DROP TABLE IF EXISTS`, and nothing
after it).

Where an engine cannot do a thing, the user is told which engine and what it
does have: DuckDB keeps whole transactions but has no savepoints; SQLite (a D1
snapshot) has both; a live Cloudflare D1 answers over stateless HTTP and has
neither — that last gate is `datasources.capabilities_for`, which the session
applies before calling anything here.
"""

from __future__ import annotations

import ibis
import ibis.expr.operations as ops

from .expression import PayloadError

__all__ = [
    "begin",
    "commit",
    "create_temp",
    "drop_temp",
    "engine_of",
    "release",
    "rollback",
    "rollback_to",
    "savepoint",
    "temp_handle",
]


def engine_of(con) -> str:
    """The engine behind an ibis connection, by the API its own handle exposes."""
    raw = _raw(con)
    if hasattr(raw, "begin"):  # DuckDB's Python connection
        return "duckdb"
    if hasattr(raw, "commit"):  # sqlite3's connection (a D1 snapshot)
        return "sqlite"
    return "unknown"


def _raw(con):
    """The backend's own connection — the one every query runs on."""
    raw = getattr(con, "con", None)
    if raw is None:
        raise PayloadError("this backend exposes no connection to control")
    return raw


def begin(con) -> None:
    """Open a transaction; it stays open until a `commit` or `rollback`."""
    if engine_of(con) == "sqlite":
        # sqlite3's handle has no `begin()`: the snapshot connection is opened
        # in autocommit mode, so the statement is exactly the transaction
        # control SQLite documents.
        _statement(con, "BEGIN", "cannot open a transaction")
        return
    _raw(con).begin()


def commit(con) -> None:
    """Commit the open transaction."""
    _statement(con, "COMMIT", "cannot commit")


def rollback(con) -> None:
    """Roll the open transaction back."""
    _statement(con, "ROLLBACK", "cannot roll back")


def savepoint(con, name: str) -> None:
    """Mark a savepoint in the open transaction."""
    _savepoint_statement(con, f"SAVEPOINT {name}", f'savepoint "{name}"')


def rollback_to(con, name: str) -> None:
    """Rewind the open transaction to a savepoint, which stays usable."""
    _savepoint_statement(con, f"ROLLBACK TO {name}", f'rollback to savepoint "{name}"')


def release(con, name: str) -> None:
    """Release a savepoint, keeping the work it covered."""
    _savepoint_statement(con, f"RELEASE {name}", f'savepoint "{name}"')


def create_temp(con, name: str, expr) -> None:
    """(Re)create a temp table holding `expr`'s rows.

    A temp table is a *write* like any other: the DDL and the rows it inserts
    both take part in an open transaction, and a rollback takes them back out.
    """
    if name.lower().startswith(("sqlite_", "_cf_")):
        raise PayloadError(f'"{name}" is reserved — pick another temp name')
    table = _temp_name(con, name)
    sql = str(ibis.to_sql(expr))
    _statement(con, f'DROP TABLE IF EXISTS {table}', f'cannot replace temp table "{name}"')
    _statement(
        con,
        f'CREATE TEMPORARY TABLE {table} AS {sql}',
        f'cannot create temp table "{name}"',
    )


def drop_temp(con, name: str) -> None:
    """Drop only from the temp namespace, even if the caller's registry is stale."""
    table = _temp_name(con, name)
    _statement(con, f'DROP TABLE IF EXISTS {table}', f'cannot drop temp table "{name}"')


def _temp_name(con, name: str) -> str:
    """Qualify every DDL target so name resolution cannot reach persistent tables."""
    engine = engine_of(con)
    if engine == "sqlite":
        return f'"temp"."{name}"'
    if engine == "duckdb":
        # DuckDB's temp catalog has its own main schema, as in ibis.create_table.
        return f'"temp"."main"."{name}"'
    raise PayloadError(f"the {engine} engine has no supported temp namespace")


def temp_handle(con, name: str, schema) -> object:
    """A table handle for one of our temp tables, with its schema supplied.

    `con.table(name)` would ask the backend to read the table's schema, and on
    SQLite that read runs in a transaction of its own — committing the session's
    open one. The schema is already known here, because it is the query's own
    output, so the handle is built from it directly; this is the same expression
    ibis itself returns from a `create_table`.
    """
    return ops.DatabaseTable(
        name, schema, source=con, namespace=ops.Namespace(database=None)
    ).to_expr()


def _savepoint_statement(con, statement: str, label: str) -> None:
    """Savepoints are the one statement an engine may simply not have."""
    engine = engine_of(con)
    if engine != "sqlite":
        raise PayloadError(
            f"the {engine} engine has no savepoints — it keeps whole transactions "
            "(\\begin, \\commit, \\rollback)"
        )
    _statement(con, statement, label)


def _statement(con, statement: str, label: str) -> None:
    """Run one connection-level statement on the backend's own handle."""
    try:
        _raw(con).execute(statement)
    except Exception as exc:  # backend-specific statement errors
        raise PayloadError(f"{label}: {exc}") from exc
