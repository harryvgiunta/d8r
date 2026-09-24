"""Cloudflare D1 over its HTTP API — the "live D1" seam.

A D1 database *is* SQLite: Cloudflare runs a real SQLite engine and answers SQL
through a small REST API (`/accounts/<id>/d1/database/<db>/query|raw`). So a
remote D1 source reuses every piece of anyQL's query machinery unchanged: the
payload is built against *unbound* ibis tables carrying the D1 database's real
schemas, compiled to the SQLite dialect, and the resulting SQL text is POSTed to
the API — which runs it on the actual D1 engine and answers with the rows.

This module owns the HTTPS round-trip and schema introspection. User queries
come from `anyql.engine.expression`; discovery issues read-only SQLite SQL.

Credentials are request-scoped and never persisted: the API token lives only in
the in-memory source that "Add data source" builds, is used per query, and is
never returned to the UI or written anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import batched
from urllib.parse import quote
from uuid import UUID

import httpx
import ibis

D1_API_ROOT = "https://api.cloudflare.com/client/v4"
# Cloudflare's SQLite runtime caps UNION/INTERSECT/EXCEPT at five terms.
_COUNT_BATCH_SIZE = 5


class D1Error(Exception):
    """A D1-side failure the user must see (bad token, unknown database, SQL error)."""


def _quote(identifier: str) -> str:
    """Safely double-quote a SQLite identifier (D1 runs SQLite; `""` escapes `"`)."""
    return '"' + identifier.replace('"', '""') + '"'


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


def _ibis_type(declared: str, notnull: int) -> str:
    name = (declared or "").strip().upper().split("(")[0].strip()
    for keys, dtype in _TYPE_MAP.items():
        if name in keys or any(name.startswith(k) for k in keys):
            null = "!" if notnull else ""
            return f"{null}{dtype}"
    return "!string" if notnull else "string"


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
        """Run SQL, returning each row as an object (for introspection/counts)."""
        results = self._post("query", sql).get("results") or []
        return [row for row in results if isinstance(row, dict)]

    def raw(self, sql: str) -> tuple[list[str], list[list]]:
        """Run SQL, returning ordered columns + row arrays (for result pages)."""
        results = self._post("raw", sql).get("results") or {}
        columns = [str(c) for c in (results.get("columns") or [])]
        rows = [list(r) for r in (results.get("rows") or [])]
        return columns, rows

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

    def schemas(self) -> dict[str, dict[str, str]]:
        """table → {column: ibis dtype} for every user table/view, in one query.

        Correlated `pragma_table_info(m.name)` pulls every table's columns in one
        round-trip; D1's authorizer refuses pragmas reaching the internal
        `sqlite_*`/`_cf_*` objects, so those are filtered out of the scan — the
        same set D1's own console hides.
        """
        sql = (
            'select m.name as source_table, p.cid as cid, p.name as name, p.type as type, p."notnull" as "notnull" '
            "from sqlite_schema m join pragma_table_info(m.name) p "
            "where m.type in ('table', 'view') "
            "and substr(m.name, 1, 7) != 'sqlite_' and substr(m.name, 1, 4) != '_cf_' "
            "order by m.name, p.cid"
        )
        tables: dict[str, dict[str, str]] = {}
        for row in self.query(sql):
            table = row.get("source_table")
            column = row.get("name")
            if not table or not column:
                continue
            cols = tables.setdefault(table, {})
            cols[str(column)] = _ibis_type(str(row.get("type") or ""), int(row.get("notnull") or 0))
        return tables

    def row_counts(self, tables: list[str]) -> dict[str, int]:
        """Count every table in batches within Cloudflare's five-term SQL limit."""
        counts: dict[str, int] = {}
        for batch in batched(tables, _COUNT_BATCH_SIZE):
            parts = (f"select {_lit(t)} as dataset, count(*) as n from {_quote(t)}" for t in batch)
            for row in self.query(" union all ".join(parts)):
                counts[str(row.get("dataset"))] = int(row.get("n") or 0)
        return counts

    def close(self) -> None:
        self._client.close()


def _lit(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


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


def schema_connection(schemas: dict[str, dict[str, str]]):
    """A read-only ibis "connection" over unbound tables carrying real schemas.

    A schema pane and every payload compile run against this exactly like a
    bound connection — only `.execute()` would fail (there is no local data),
    which is why remote queries compile to SQLite SQL and run it through the
    D1 API instead.
    """
    return _UnboundCon(schemas)


class _UnboundCon:
    def __init__(self, schemas: dict[str, dict[str, str]]) -> None:
        self._schemas = schemas

    def table(self, name: str):
        try:
            schema = self._schemas[name]
        except KeyError:
            raise KeyError(name) from None
        return ibis.table(name=name, schema=ibis.schema(schema))

    def list_tables(self) -> list[str]:
        return list(self._schemas)
