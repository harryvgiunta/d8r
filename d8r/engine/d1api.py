"""Cloudflare D1 over its HTTP API — the "live D1" seam.

A D1 database *is* SQLite: Cloudflare runs a real SQLite engine and answers SQL
through a small REST API (`/accounts/<id>/d1/database/<db>/query|raw`). So a
remote D1 source reuses every piece of D8R's query machinery unchanged: the
payload is built against *unbound* ibis tables carrying the D1 database's real
schemas, compiled to the SQLite dialect, and the resulting SQL text is POSTed to
the API — which runs it on the actual D1 engine and answers with the rows.

This module owns the HTTPS round-trip and schema introspection. User queries
come from `d8r.engine.expression`; discovery issues read-only SQLite SQL.

The client holds the token in memory and uses it per request; it never persists
or renders credentials. Explicit Add in the TUI saves credentials through the
local storage layer, independently of this transport.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from threading import RLock
from urllib.parse import quote
from uuid import UUID

import httpx
import ibis

from d8r.query.schema import ForeignKey

D1_API_ROOT = "https://api.cloudflare.com/client/v4"


class D1Error(Exception):
    """A D1-side failure the user must see (bad token, unknown database, SQL error)."""


# Declared-type (as D1/sqlite reports it) → ibis dtype. Unknown names fall back
# to string; ibis only needs this to type the compiled SQL, and a wrong guess
# never corrupts a read (D1 returns the stored value regardless).
_TYPE_MAP: dict[tuple[str, ...], str] = {
    ("INTEGER", "INT", "BIGINT", "TINYINT", "SMALLINT"): "int64",
    ("REAL", "DOUBLE", "FLOAT", "NUMERIC", "DECIMAL"): "float64",
    ("TEXT", "CLOB", "VARCHAR", "CHAR"): "string",
    ("BLOB", "ANY"): "binary",
    ("DATE",): "date",
    ("DATETIME", "TIMESTAMP"): "timestamp",
    ("BOOLEAN", "BOOL"): "boolean",
}
# Prefix rules ordered longest-key-first, so a longer name never loses to a
# shorter prefix of itself (DATETIME must not match DATE, CHARACTER not CHAR).
_TYPE_PREFIXES: tuple[tuple[str, str], ...] = tuple(
    sorted(
        ((key, dtype) for keys, dtype in _TYPE_MAP.items() for key in keys),
        key=lambda pair: len(pair[0]),
        reverse=True,
    )
)


def _ibis_type(declared: str, notnull: int) -> str:
    name = (declared or "").strip().upper().split("(")[0].strip()
    for key, dtype in _TYPE_PREFIXES:
        if name == key or name.startswith(key):
            null = "!" if notnull else ""
            return f"{null}{dtype}"
    return "!string" if notnull else "string"


def _sqlite_objects(names: list[str] | None) -> str:
    sql = (
        "where m.type in ('table', 'view') "
        "and substr(m.name, 1, 7) != 'sqlite_' and substr(m.name, 1, 4) != '_cf_' "
    )
    if names is not None:
        literals = ", ".join("'" + name.replace("'", "''") + "'" for name in names)
        sql += f"and m.name in ({literals}) "
    return sql


def sqlite_foreign_keys(
    query: Callable[[str], Iterable[dict]], names: list[str] | None = None,
) -> dict[str, tuple[ForeignKey, ...]]:
    """Read only SQLite catalogs; resolve omitted targets by primary-key ordinal."""
    if names is not None and not names:
        return {}
    sql = (
        'select m.name as source_table, f.id as constraint_id, f.seq as position, '
        'f."from" as source_column, f."table" as target_table, '
        'coalesce(f."to", p.name) as target_column, '
        'case when f."to" is null then '
        '(select count(*) from pragma_table_info(f."table") where pk > 0) '
        'end as target_pk_count '
        'from sqlite_schema m join pragma_foreign_key_list(m.name) f '
        'left join pragma_table_info(f."table") p on f."to" is null and p.pk = f.seq + 1 '
    )
    sql += _sqlite_objects(names) + "order by m.name, f.id, f.seq"
    groups: dict[tuple[str, int], list[dict]] = {}
    for row in query(sql):
        groups.setdefault((row["source_table"], row["constraint_id"]), []).append(row)
    tables: dict[str, list[ForeignKey]] = {}
    for (table, _), rows in groups.items():
        targets = tuple(row["target_column"] for row in rows)
        if any(target is None for target in targets) or any(
            row["target_pk_count"] is not None and row["target_pk_count"] != len(rows)
            for row in rows
        ):
            targets = ()
        tables.setdefault(table, []).append(ForeignKey(
            tuple(row["source_column"] for row in rows), rows[0]["target_table"], targets,
        ))
    return {table: tuple(keys) for table, keys in tables.items()}


class _SchemaSnapshot(dict[str, dict[str, str]]):
    """Column/FK metadata from one successful discovery, published together."""

    def __init__(self, schemas: dict[str, dict[str, str]], foreign_keys: dict[str, tuple[ForeignKey, ...]]):
        super().__init__(schemas)
        self.foreign_keys = foreign_keys


@dataclass
class CloudflareD1:
    """A live Cloudflare D1 connection with an in-memory token and database uuid.

    `transport` lets tests inject an `httpx.MockTransport` so the real
    request/response handling is exercised with no network.
    """

    account_id: str
    api_token: str = field(repr=False)
    database: str  # what the user typed: a database name or a uuid
    database_uuid: str = ""  # resolved uuid (empty until `_resolve`)
    _client: httpx.Client = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        for label, value in (("Account ID", self.account_id), ("API token", self.api_token), ("Database", self.database)):
            if not (value or "").strip():
                raise D1Error(f"{label} is required")
        self.account_id = self.account_id.strip()
        self.api_token = self.api_token.strip()
        self.database = self.database.strip()
        self._client = self._client or httpx.Client(timeout=30.0)

    # -- transport --------------------------------------------------------
    def _post(self, endpoint: str, sql: str) -> dict:
        url = f"{D1_API_ROOT}/accounts/{quote(self.account_id, safe='')}/d1/database/{self._db()}/{endpoint}"
        try:
            resp = self._client.post(url, headers=self._headers(), json={"sql": sql})
        except httpx.HTTPError:
            raise D1Error("Could not reach Cloudflare. Check your network connection and try again.") from None
        return self._result(resp)

    def _get(self, path: str, params: dict) -> list[dict]:
        url = f"{D1_API_ROOT}{path}"
        try:
            resp = self._client.get(url, headers=self._headers(), params=params)
        except httpx.HTTPError:
            raise D1Error("Could not reach Cloudflare. Check your network connection and try again.") from None
        result = self._envelope(resp).get("result")
        if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
            raise D1Error("Cloudflare D1 returned an invalid database list")
        return result

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_token}"}

    def _db(self) -> str:
        return self.database_uuid or self.resolve()

    def _envelope(self, resp: httpx.Response) -> dict:
        try:
            body = resp.json()
        except ValueError as exc:
            raise D1Error(f"invalid response from Cloudflare (HTTP {resp.status_code})") from exc
        if not isinstance(body, dict):
            raise D1Error(f"invalid response from Cloudflare (HTTP {resp.status_code})")
        if resp.status_code != 200 or body.get("success") is not True:
            raise D1Error(self._safe_error(body, resp.status_code))
        return body

    def _result(self, resp: httpx.Response) -> dict:
        body = self._envelope(resp)
        items = body.get("result")
        if not isinstance(items, list) or not items:
            raise D1Error("Cloudflare D1 returned no result")
        item = items[0]
        if not isinstance(item, dict):
            raise D1Error("Cloudflare D1 returned an invalid result")
        if item.get("success") is False:
            raise D1Error(self._safe_error(item, resp.status_code))
        return item

    def _safe_error(self, node: dict, status: int) -> str:
        return _api_error(node, status).replace(self.api_token, "[redacted]")

    # -- statements -------------------------------------------------------
    def query(self, sql: str) -> list[dict]:
        """Run SQL, returning each row as an object (for introspection)."""
        results = self._post("query", sql).get("results") or []
        return [row for row in results if isinstance(row, dict)]

    def raw(self, sql: str) -> tuple[list[str], list[list]]:
        """Run SQL, returning ordered columns + row arrays (for result pages)."""
        results = self._post("raw", sql).get("results") or {}
        columns = [str(c) for c in (results.get("columns") or [])]
        rows = [list(r) for r in (results.get("rows") or [])]
        return columns, rows

    def check_connection(self) -> None:
        """Authenticate against the resolved database without discovering its schema."""
        self.query("SELECT 1")

    # -- introspection ----------------------------------------------------
    def resolve(self) -> str:
        """Resolve a database name to a UUID; a binding is not an API identifier."""
        if _looks_like_uuid(self.database):
            self.database_uuid = str(UUID(self.database))
            return self.database_uuid
        page = 1
        while True:
            rows = self._get(
                f"/accounts/{quote(self.account_id, safe='')}/d1/database",
                {"name": self.database, "page": page, "per_page": 1000},
            )
            for row in rows:
                if row.get("name") == self.database:
                    uuid = str(row.get("uuid") or "")
                    if not _looks_like_uuid(uuid):
                        raise D1Error("Cloudflare returned a database without a valid UUID")
                    self.database_uuid = str(UUID(uuid))
                    return self.database_uuid
            if len(rows) < 1000:
                break
            page += 1
        raise D1Error(
            "No D1 database matched that name in this account. Check the Account ID "
            "and database name, or paste the database UUID from Workers & Pages → D1."
        )

    def schemas(self, names: list[str] | None = None) -> dict[str, dict[str, str]]:
        """Return ordered column types for all or only the named user tables/views.

        Filter `sqlite_schema` before the correlated `pragma_table_info(m.name)`
        introspection. D1's authorizer rejects internal `sqlite_*`/`_cf_*`
        objects; a named lookup must also leave unrelated user objects alone.
        """
        if names is not None and not names:
            return {}
        sql = (
            'select m.name as source_table, p.cid as cid, p.name as name, p.type as type, p."notnull" as "notnull" '
            "from sqlite_schema m join pragma_table_info(m.name) p "
        )
        sql += _sqlite_objects(names) + "order by m.name, p.cid"
        tables: dict[str, dict[str, str]] = {}
        for row in self.query(sql):
            table = row.get("source_table")
            column = row.get("name")
            if not table or not column:
                continue
            cols = tables.setdefault(table, {})
            cols[str(column)] = _ibis_type(str(row.get("type") or ""), int(row.get("notnull") or 0))
        return _SchemaSnapshot(tables, sqlite_foreign_keys(self.query, list(tables)))

    def close(self) -> None:
        self._client.close()


def _looks_like_uuid(text: str) -> bool:
    try:
        UUID(text)
    except ValueError:
        return False
    return True


def _api_error(node: dict, status: int) -> str:
    """Pull a human message out of a Cloudflare error envelope (errors[] or detail)."""
    errors = node.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        message = errors[0].get("message")
        if message:
            return f"Cloudflare D1: {message}"
    for key in ("detail", "error", "message"):
        value = node.get(key)
        if isinstance(value, str) and value:
            return f"Cloudflare D1: {value}"
    return f"Cloudflare D1 request failed (HTTP {status})"


def schema_connection(schemas: dict[str, dict[str, str]], *, d1: CloudflareD1 | None = None):
    """A read-only ibis connection with optional on-demand D1 table metadata.

    With no `d1`, the connection stays entirely offline. Supplying a client
    loads only missing tables, while `.seed()` accepts separately indexed
    metadata without performing network requests.
    """
    return _UnboundCon(schemas, d1=d1)


class _UnboundCon:
    def __init__(self, schemas: dict[str, dict[str, str]], *, d1: CloudflareD1 | None = None) -> None:
        self._schemas: dict[str, dict[str, str]] = {}
        self._foreign_keys: dict[str, tuple[ForeignKey, ...]] = {}
        self._d1 = d1
        self._lock = RLock()
        self.seed(schemas)

    def table(self, name: str):
        if name.startswith(("sqlite_", "_cf_")):
            raise KeyError(name)
        with self._lock:
            schema = self._schemas.get(name)
        if schema is None and self._d1 is not None:
            # Never hold the cache lock across HTTP: another table's lookup or
            # the application's background index must not gate this request.
            self.seed(self._d1.schemas([name]))
            with self._lock:
                schema = self._schemas.get(name)
        if schema is None:
            raise KeyError(name)
        return ibis.table(name=name, schema=ibis.schema(schema))

    def seed(self, schemas: dict[str, dict[str, str]]) -> None:
        snapshot = {name: dict(columns) for name, columns in schemas.items()}
        foreign_keys = getattr(schemas, "foreign_keys", {})
        with self._lock:
            self._schemas.update(snapshot)
            for name in snapshot:
                self._foreign_keys[name] = tuple(foreign_keys.get(name, ()))

    def list_tables(self) -> list[str]:
        with self._lock:
            return list(self._schemas)

    def foreign_keys(self, name: str) -> tuple[ForeignKey, ...]:
        """Return cached constraints only; never discover metadata from the UI."""
        with self._lock:
            return self._foreign_keys.get(name, ())
