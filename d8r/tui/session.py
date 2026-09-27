"""The TUI's headless core: registry, schema seam, palette data, runs, history.

Everything the widgets need to know is computed here in plain Python, so the
Textual layer stays a thin shell and the tests can drive a `Session` without a
terminal attached. Provider configuration is loaded from local settings; AI
transport lives in `d8r.ai`, while database networking stays in the engine.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import RLock
from uuid import uuid4

from d8r.ai.client import AIConfig

from d8r.engine import (
    D1Error,
    DIALECT_BY_NAME,
    DataSource,
    PayloadError,
    add_d1_live_source,
    add_postgres_source,
    add_sqlite_source,
    begin,
    build,
    commit,
    capabilities_for,
    column_values,
    compile_sql,
    dialect_for,
    drop_temp,
    execute,
    execute_remote,
    load,
    materialize,
    release,
    rollback,
    rollback_to,
    savepoint,
    temp_handle,
    type_name,
)
from d8r.query import (
    Capabilities,
    ColumnDef,
    QueryAST,
    SchemaContext,
    TableDef,
    FnDef,
    is_identifier,
    open_tables_of,
    parse_body,
    parse_query,
    payload_from_ast,
)
from d8r.storage import MemoryStore, SettingsStore, WorkspaceStore

PREVIEW_ROW_CAP = 10000
"""Rows the results DataTable buffers after query execution.

This UI cap is independent of the user-configured default query limit. It does
not rewrite the query; the reported total describes the executed expression.
"""

VALUE_POOL_LIMIT = 1000
"""Distinct values fetched per column for the `\\where` value search.

The palette shows `palette.VALUE_SUGGESTIONS` of them at a time; the pool is
what the typing searches, so a value that sorts past the first page is still
found by typing part of it. One fetch per (source, dataset, column), cached.
"""


# ---------------------------------------------------------------------------
# Source -> schema seam
# ---------------------------------------------------------------------------


def tables_of(source: DataSource) -> list[TableDef]:
    """The source's datasets as registry tables (live ibis schemas, in order)."""
    tables: list[TableDef] = []
    for name, entry in source.datasets.items():
        schema = entry["table"].schema()
        columns = [ColumnDef(col, type_name(dtype)) for col, dtype in schema.items()]
        tables.append(TableDef(name=name, doc=entry.get("doc", ""), columns=columns))
    return tables


def capabilities_object(source: DataSource) -> Capabilities:
    """`capabilities_for(source)` as the registry's `Capabilities` record."""
    caps = capabilities_for(source)
    return Capabilities(
        backend=caps["backend"],
        aggregates=caps["aggregates"],
        functions=caps["functions"],
        operators=caps["operators"],
        window_functions=caps.get("windowFunctions", ()),
        supports=caps.get("supports", {}),
    )


def default_dialect(source: DataSource) -> str:
    """The compile dialect a source suggests — or duckdb when it cannot render."""
    spec = DIALECT_BY_NAME.get(source.dialect)
    return source.dialect if spec is not None and spec["compiles"] else "duckdb"


def _source_id(hint: str, taken) -> str:
    """A stable, unique registry id derived from a human hint."""
    base = re.sub(r"[^a-z0-9]+", "-", hint.strip().lower()).strip("-") or "source"
    candidate = base
    suffix = 2
    while candidate in taken:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def looks_numeric(value: str) -> bool:
    r"""True for a value `\where` can carry bare; text gets quoted instead."""
    try:
        float(value)
    except ValueError:
        return False
    return True


def has_query(ast: QueryAST) -> bool:
    r"""True when the document carries a query, not only its statements.
    `\begin`/`\commit`/`\rollback`/`\savepoint`/`\release` and `\drop` are
    statements: a document of theirs alone runs them and answers with their
    status. Everything else — `\with` bodies included — makes a query that the
    statements wrap around.
    """
    return bool(
        ast.with_
        or ast.from_
        or ast.joins
        or ast.set_ops
        or ast.select
        or ast.distinct
        or ast.where
        or ast.group_by
        or ast.order_by
        or ast.cases
        or ast.limit is not None
    )


# ---------------------------------------------------------------------------
# Results of one run
# ---------------------------------------------------------------------------


@dataclass
class HistoryEntry:
    """One executed document, as the History tab lists it."""

    at: str
    source: str
    dialect: str
    rows: int
    ms: float
    doc: str
    target: str = ""

    @property
    def title(self) -> str:
        """The document's first non-empty line, for the History table."""
        for line in self.doc.split("\n"):
            if line.strip():
                return line.strip()
        return ""


@dataclass
class RunOutcome:
    """What one `Run` produced: a rendered result, or just a message.

    `error` is the Results-tab line (empty when the run succeeded); `status` is
    always what the footer shows. `rows` is already capped at `PREVIEW_ROW_CAP`
    while `total` is what the document asked for. `schema_changed` says the run
    added or removed a table — a `\\temp`/`\\drop` — so the explorer and the
    parser's schema seam need re-reading.
    """

    status: str = ""
    error: str = ""
    ok: bool = False
    source: str = ""
    dialect: str = ""
    columns: list[str] = field(default_factory=list)
    dtypes: list[str] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    total: int = 0
    ms: float = 0.0
    sql: str = ""
    schema_changed: bool = False

    @property
    def capped(self) -> bool:
        """True when the preview buffer, not the document, decided the row count."""
        return self.total > len(self.rows)


@dataclass
class TxState:
    """One source's transaction: whether it is open, and the savepoints in it.

    The transaction itself lives on the connection; this is what the session
    needs to answer with (which savepoints exist, whether `\\begin` is legal
    yet) and it dies with the process; it is never part of saved local memory.
    """

    open: bool = False
    savepoints: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# The session
# ---------------------------------------------------------------------------


class Session:
    """The app's state: which source is live, what dialect, what has run.

    Constructing one loads the bundled engine registry and captures its schema,
    so a session is always ready to parse against real columns without changing
    the language layer or any other session.
    """

    def __init__(
        self, sources: dict[str, DataSource] | None = None, *, data_dir: Path | None = None,
    ) -> None:
        self.sources: dict[str, DataSource] = load() if sources is None else dict(sources)
        self.active_id: str = next(iter(self.sources))
        self.dialect: str = default_dialect(self.source)
        # UI-owned reservation: a background run/context lookup owns the shared
        # connection until its real work finishes. Headless
        # callers keep the synchronous run API and need no reservation.
        self.busy: str = ""
        # One transaction per source: what `\begin` opened lives on that
        # source's connection, so it is tracked per source and never leaks
        # into another one.
        self.tx: dict[str, TxState] = {}
        # Temp tables a `\drop` removed, per source: a rollback undoes the
        # drop on the engine, and this is what puts the registry back in step.
        self._dropped: dict[str, dict[str, dict]] = {}
        self._values: dict[tuple[str, str, str], list[str]] = {}
        # Definitions load without schema-dependent validation: a D1 function
        # remains available even while the bundled demo is the active source.
        self._memory = MemoryStore(data_dir)
        self.fns: dict[str, FnDef] = {
            item["name"]: FnDef(
                name=item["name"], params=item["params"], body=item["body"], doc=item["description"],
            )
            for item in self._memory.document["functions"]
        }
        self.d1_profiles: list[dict[str, str]] = self._memory.document["d1_profiles"]
        self.postgres_profiles: list[dict[str, str]] = self._memory.document["postgres_profiles"]
        self._settings = SettingsStore(self._memory.path.parent)
        settings = self._settings.document
        if settings["source"] in self.sources:
            self.active_id = settings["source"]
            self.dialect = settings["dialect"]
        self.intellisense: bool = settings["intellisense"]
        self.default_rows: int = settings["default_rows"]
        self.pane_visibility: dict[str, bool] = dict(settings["panes"])
        self.ai_config = AIConfig(**settings["ai"])
        self._workspace = WorkspaceStore(self._memory.path.parent)
        self._workspace_lock = RLock()
        self.workspace = deepcopy(self._workspace.document)
        self.workspace_error = self._workspace.error
        self.workspace["document_id"] = self.workspace["document_id"] or uuid4().hex
        self.history = [HistoryEntry(**entry) for entry in self.workspace["history"]]
        if self.workspace["source"]:
            self.restore_source(self.workspace["source"], self.workspace["dialect"] or None)
        else:
            self.workspace["source"] = self.source_key()
            self.workspace["dialect"] = self.dialect
            self.refresh_schema()

    @property
    def storage_path(self) -> Path:
        """The local JSON file containing functions and saved database credentials."""
        return self._memory.path

    @property
    def settings_path(self) -> Path:
        """The editable JSON file containing preferences and AI configuration."""
        return self._settings.path

    @property
    def memory_error(self) -> str:
        """An actionable startup read error, or an empty string."""
        return "\n".join(error for error in (self._memory.error, self._settings.error, self.workspace_error) if error)

    def update_settings(
        self, *, intellisense: bool | None = None,
        panes: dict[str, bool] | None = None, source: str | None = None,
        dialect: str | None = None, ai: AIConfig | None = None,
        default_rows: int | None = None,
    ) -> None:
        """Save explicit preference changes before applying them to the session.

        Temporary source contexts and unsaved provider forms never call this.
        A failed write leaves both the live state and the saved file untouched.
        """
        document = dict(self._settings.document)
        if intellisense is not None:
            document["intellisense"] = intellisense
        if panes is not None:
            document["panes"] = {**document["panes"], **panes}
        if source is not None:
            if source not in self.sources:
                raise ValueError("The selected data source is not registered.")
            if dialect is None:
                dialect = default_dialect(self.sources[source])
            document["source"] = source
        if dialect is not None:
            document["dialect"] = dialect
        if ai is not None:
            document["ai"] = asdict(ai)
        if default_rows is not None:
            document["default_rows"] = default_rows
        self._settings.save(document)
        if intellisense is not None:
            self.intellisense = intellisense
        if panes is not None:
            self.pane_visibility = dict(self._settings.document["panes"])
        if source is not None:
            self.set_active(source)
        if dialect is not None:
            self.dialect = dialect
        if ai is not None:
            self.ai_config = ai
        if default_rows is not None:
            self.default_rows = default_rows

    @property
    def workspace_path(self) -> Path:
        return self._workspace.path

    def save_workspace(self, **changes) -> None:
        """Merge owned fields under one lock; failures preserve the live draft."""
        with self._workspace_lock:
            document = {**self.workspace, **deepcopy(changes)}
            sources = dict(document["sources"])
            for source in self.sources.values():
                if source.con is not None:
                    sources[self.source_key(source.id)] = {
                        "id": source.id, "display": source.display, "kind": source.kind,
                        "path": str(source.dir.expanduser().resolve()) if source.kind == "d1" else "",
                    }
            document["sources"] = sources
            self.workspace = document
            if document == self._workspace.document and not self.workspace_error:
                return
            try:
                self._workspace.save(document)
            except ValueError as exc:
                self.workspace_error = str(exc)
                raise
            self.workspace_error = ""

    def load_chat(self, key: str) -> dict | None:
        with self._workspace_lock:
            return deepcopy(self.workspace["chats"].get(key))

    def save_chat(self, key: str, chat: dict | None) -> None:
        with self._workspace_lock:
            chats = dict(self.workspace["chats"])
            if chat is None:
                chats.pop(key, None)
            else:
                chats[key] = deepcopy(chat)
            self.save_workspace(chats=chats)

    def _record_history(self, entry: HistoryEntry, outcome: RunOutcome) -> None:
        with self._workspace_lock:
            self.history.insert(0, entry)
            try:
                self.save_workspace(history=[asdict(item) for item in self.history])
            except ValueError as exc:
                # The query already ran. A disk error must not suggest a retry
                # of potentially stateful work or throw away its result.
                outcome.status += f" · history not saved: {exc}"

    def source_key(self, source_id: str | None = None) -> str:
        source_id = self.active_id if source_id is None else source_id
        source = self.sources.get(source_id)
        if source is None or source.con is None:
            return source_id
        if source.d1 is not None:
            return f"saved-d1:{source.d1.account_id}:{source.d1.database_uuid.lower()}"
        if source.kind == "d1":
            return f"snapshot:{source.dir.expanduser().resolve()}"
        if source.kind == "postgres-live":
            return self._postgres_profile_id(source.postgres)
        return source_id

    def source_connected(self, source_id: str | None = None) -> bool:
        source = self.sources.get(self.active_id if source_id is None else source_id)
        return source is not None and source.con is not None

    def saved_snapshot_path(self, source_id: str) -> str | None:
        source = self.workspace["sources"].get(self.source_key(source_id))
        return source["path"] if source and source["kind"] == "d1" and source["path"] else None

    def restore_source(self, key: str, dialect: str | None = None, *, activate: bool = True) -> str:
        """Restore target identity, never open a snapshot or contact a service."""
        source_id = next((name for name in self.sources if self.source_key(name) == key), None)
        if source_id is None:
            saved = self.workspace["sources"].get(key, {})
            profile = self.saved_source_profile(key)
            label = saved.get("display") or (profile["display"] if profile else key)
            source_id = key
            self.sources[key] = DataSource(
                id=key, display=f"{label} · disconnected", doc="Reconnect explicitly to use this target.",
                kind="disconnected", dialect=dialect or ("postgres" if key.startswith("saved-postgres:") else "sqlite"), dir=Path(saved.get("path") or "."),
            )
        if activate:
            self.set_active(source_id)
            if dialect:
                self.dialect = dialect
        return source_id

    def _query_payload(self, ast: QueryAST) -> dict:
        payload = payload_from_ast(ast)
        if not ast.temp and payload.get("limit") is None and self.default_rows:
            payload["limit"] = self.default_rows
        return payload

    def _save_memory(self, fns: dict[str, FnDef], profiles: list[dict[str, str]],
                     postgres_profiles: list[dict[str, str]] | None = None) -> None:
        self._memory.save([
            {"name": fn.name, "params": list(fn.params), "body": fn.body, "description": fn.doc}
            for fn in fns.values()
        ], profiles, self.postgres_profiles if postgres_profiles is None else postgres_profiles)

    def remember_d1(
        self, account_id: str, database: str, display: str, api_token: str = "",
    ) -> None:
        """Persist connection identifiers and the explicitly supplied API token."""
        profile = {"account_id": account_id, "database": database, "display": display,
                   "api_token": api_token}
        profiles = list(self.d1_profiles)
        for index, saved in enumerate(profiles):
            if saved["account_id"] == account_id and saved["database"] == database:
                profiles[index] = profile
                break
        else:
            profiles.append(profile)
        self._save_memory(self.fns, profiles)
        self.d1_profiles = profiles

    def remember_postgres(self, profile: dict[str, str]) -> None:
        """Save credentials only following explicit Add; failure keeps prior state."""
        profile = {key: value for key, value in profile.items() if key != "kind"}
        profile["port"] = str(int(profile["port"]))
        identity = self._postgres_profile_id(profile)
        profiles = list(self.postgres_profiles)
        for index, saved in enumerate(profiles):
            if self._postgres_profile_id(saved) == identity:
                profiles[index] = dict(profile)
                break
        else:
            profiles.append(dict(profile))
        self._save_memory(self.fns, self.d1_profiles, profiles)
        self.postgres_profiles = profiles

    @staticmethod
    def _postgres_profile_id(profile: dict[str, str]) -> str:
        # JSON preserves boundaries in arbitrary database/user/schema identifiers.
        identity = [profile[key] for key in ("host", "port", "database", "user", "schema", "sslmode")]
        return "saved-postgres:" + json.dumps(identity, ensure_ascii=False, separators=(",", ":"))

    # -- the active source --------------------------------------------------

    @property
    def source(self) -> DataSource:
        """The active datasource."""
        return self.sources[self.active_id]

    @staticmethod
    def _profile_id(profile: dict[str, str]) -> str:
        # Connected source IDs are slugs; this namespace cannot collide with one.
        return f"saved-d1:{profile['account_id']}:{profile['database'].lower()}"

    def saved_source_profile(self, source_id: str) -> dict[str, str] | None:
        """Resolve a saved picker entry without connecting or changing schemas."""
        postgres = next((profile for profile in self.postgres_profiles
                         if self._postgres_profile_id(profile) == source_id), None)
        if postgres is not None:
            return {**postgres, "kind": "postgres-live"}
        return next((profile for profile in self.d1_profiles
                     if self._profile_id(profile) == source_id), None)

    def source_options(self) -> list[tuple[str, str]]:
        """Connected sources plus saved profiles not already connected."""
        connected = {(source.d1.account_id, source.d1.database_uuid.lower())
                     for source in self.sources.values() if source.d1 is not None}
        registered = {self.source_key(name) for name in self.sources}
        return [(source.display, source.id) for source in self.sources.values()] + [
            (f"{profile['display']} · disconnected", self._profile_id(profile))
            for profile in self.d1_profiles
            if (profile['account_id'], profile['database'].lower()) not in connected
            and self._profile_id(profile) not in self.sources
        ] + [
            (f"{profile['display']} · disconnected", self._postgres_profile_id(profile))
            for profile in self.postgres_profiles
            if self._postgres_profile_id(profile) not in registered
        ] + [
            (f"{source['display']} · disconnected", key)
            for key, source in self.workspace["sources"].items()
            if source["kind"] == "d1" and key not in registered
        ]

    def set_active(self, source_id: str) -> None:
        """Point the session at another registered source and capture its schema."""
        if source_id not in self.sources:
            raise KeyError(source_id)
        self.active_id = source_id
        self.dialect = default_dialect(self.source)
        self.refresh_schema()

    @contextmanager
    def target_source(self, source_id: str | None = None) -> Iterator[None]:
        """Use a source for synchronous editor work, then restore the workspace.

        Never hold this scope across an await: the session's active source changes.
        """
        if source_id is None or source_id == self.active_id:
            yield
            return
        active_id, dialect = self.active_id, self.dialect
        try:
            self.set_active(source_id)
            yield
        finally:
            self.active_id, self.dialect = active_id, dialect
            self.refresh_schema()

    def register(self, source: DataSource, activate: bool = True) -> None:
        """Register an explicitly built source, replacing its disconnected target."""
        key = (f"saved-d1:{source.d1.account_id}:{source.d1.database_uuid.lower()}" if source.d1 is not None else
               f"snapshot:{source.dir.expanduser().resolve()}" if source.kind == "d1" else
               self._postgres_profile_id(source.postgres) if source.kind == "postgres-live" else source.id)
        placeholders = [name for name, item in self.sources.items() if item.con is None and self.source_key(name) == key]
        was_active = self.active_id in placeholders
        for name in placeholders:
            del self.sources[name]
        self.sources[source.id] = source
        if activate:
            self.set_active(source.id)
        elif was_active:
            dialect = self.dialect
            self.set_active(source.id)
            self.dialect = dialect
        elif source.id == self.active_id:
            self.refresh_schema()

    def refresh_schema(self) -> None:
        """Replace this session's snapshot; in-flight parses retain the old one."""
        source = self.source
        self.schema = SchemaContext(
            tables=tables_of(source), capabilities=capabilities_object(source),
            fns=tuple(self.fns.values()),
        )

    # -- palette data -------------------------------------------------------

    def open_tables(self, doc: str, *, schema: SchemaContext | None = None):
        """The document's open tables (empty when it has no `\\from` yet)."""
        schema = self.schema if schema is None else schema
        ast = parse_query(doc, schema=schema)
        if ast.from_ is None:
            return []
        return open_tables_of(ast.from_, ast.joins)

    def column_entries(
        self, doc: str, *, schema: SchemaContext | None = None,
    ) -> list[tuple[str, str, str]]:
        """(column, dtype, detail) rows for the palette, open tables first.

        With a `\\from` set the rows are exactly the document's open tables, in
        open order; without one they are the registry's cross-dataset pool.
        This is the data — which rows match what is typed is the palette's rule.
        """
        schema = self.schema if schema is None else schema
        open_tables = self.open_tables(doc, schema=schema)
        rows: list[tuple[str, str, str]] = []
        if open_tables:
            for table_name in dict.fromkeys(t.dataset for t in open_tables):
                table = schema.table_by_name(table_name)
                if table is None:
                    continue
                for column in table.columns:
                    rows.append((column.name, column.type, f"{column.type} · {table_name}"))
        else:
            rows = [
                (col.name, col.type, f"{col.type} · {', '.join(col.tables)}") for col in schema.pool
            ]
        return rows

    def dataset_entries(
        self, doc: str, *, schema: SchemaContext | None = None,
    ) -> list[tuple[str, str]]:
        """(table, detail) rows for the dataset-taking palette clauses.

        The document's own `\\with` names lead: a CTE is addressable by name
        from a later `\\from`/`\\open`/`\\join`/`\\union` — and it is the table
        the user just wrote — so it belongs in the offers. Then the active
        source's datasets, in registry order; a temp table this session created
        says so instead of showing a row count that is only as fresh as the run.
        """
        schema = self.schema if schema is None else schema
        source = self.source
        datasets = [
            (name, "temp table" if entry.get("temp") else
             "row count not loaded" if entry["rows"] is None else f"{entry['rows']} rows")
            for name, entry in source.datasets.items()
        ]
        ctes = [(cte.name, "cte") for cte in parse_query(doc, schema=schema).with_]
        return [*ctes, *datasets]


    # -- saved table-valued functions --------------------------------------

    def save_fn(self, name: str, params_text: str, body: str, doc: str) -> FnDef:
        """Create or replace a saved function; `ValueError` names what is wrong.

        The name is an identifier; `params_text` is the comma-separated
        positional signature; the body is validated against the live schema
        exactly as a call would expand it (`parse_body`), so a function that
        could only fail at its call site is refused here. Saving persists first,
        then replaces the snapshot so the parser resolves a call immediately.
        """
        fn = self.validate_fn(name, params_text, body, doc)
        functions = dict(self.fns)
        functions[fn.name] = fn
        self._save_memory(functions, self.d1_profiles)
        self.fns[fn.name] = fn  # replacing keeps definition order; a new name appends
        self.refresh_schema()
        return fn

    def validate_fn(self, name: str, params_text: str, body: str, doc: str) -> FnDef:
        """Validate a candidate snapshot without saving or replacing live definitions."""
        name = name.strip()
        if not is_identifier(name):
            raise ValueError("the function name must be a bare identifier (letters, digits, _)")
        params = [part.strip() for part in params_text.split(",") if part.strip()]
        if len(set(params)) != len(params):
            raise ValueError("duplicate parameter name in the signature")
        for param in params:
            if not is_identifier(param):
                raise ValueError(f'parameter "{param}" is not a bare identifier')
        fn = FnDef(name=name, params=params, body=body, doc=doc.strip())
        schema = self.schema
        functions = dict(self.fns)
        functions[name] = fn
        candidate = SchemaContext(
            tables=schema.tables, capabilities=schema.capabilities,
            fns=tuple(functions.values()),
        )
        messages = parse_body(body, fn.params, schema=candidate, function_name=name)
        if messages:
            raise ValueError(messages[0])
        return fn

    def delete_fn(self, name: str) -> None:
        """Persist deletion before removing the function from the live registry."""
        if name not in self.fns:
            return
        functions = dict(self.fns)
        del functions[name]
        self._save_memory(functions, self.d1_profiles)
        del self.fns[name]
        self.refresh_schema()

    def fn_call_rows(self, *, schema: SchemaContext | None = None) -> list[tuple[str, str]]:
        """(call, detail) rows for the dataset-taking palette clauses.

        `name()` is what a row inserts — the caret lands between the parens —
        and the detail is the signature the library shows.
        """
        schema = self.schema if schema is None else schema
        return [
            (
                f"{fn.name}()",
                "function · "
                + (
                    ", ".join(fn.params)
                    if fn.params
                    else "no arguments"
                )
                + (f" · {fn.doc}" if fn.doc else ""),
            )
            for fn in schema.fns
        ]

    def fn_preview(self, name: str, args_text: str = "") -> RunOutcome:
        """Run a call to a saved function and show its rows, without history.

        The preview is a real document (`\\from name(args)`) run through `run`,
        so it exercises the exact call-site path the editor's own queries take —
        same expansion, same engine, same errors — with `record=False` so trying
        a function out never lands in the History pane.
        """
        doc = f"\\from {name}({args_text.strip()})\n\\select *"
        return self.run(doc, record=False)

    def values_for(
        self, doc: str, column: str, *, schema: SchemaContext | None = None,
    ) -> list[str]:
        """A column's distinct values, for the `\\where` value search.

        Cached per (source, dataset, column) and fetched up to
        `VALUE_POOL_LIMIT`, so typing can search past the first sorted page. A
        value fetch is best-effort, so a failure (a live D1 hiccup, a column with
        no value source) means no suggestions rather than an error to dismiss.
        """
        schema = self.schema if schema is None else schema
        source = self.source
        source_id = self.active_id
        dataset = self.dataset_of(doc, column, schema=schema)
        if dataset is None:
            return []
        key = (source_id, dataset, column)
        if key not in self._values:
            try:
                self._values[key] = column_values(
                    source, dataset, column, limit=VALUE_POOL_LIMIT
                )
            except (PayloadError, D1Error, OSError, ValueError, KeyError):
                self._values[key] = []
        return self._values[key]

    def dataset_of(
        self, doc: str, column: str, *, schema: SchemaContext | None = None,
    ) -> str | None:
        """The dataset a column reference resolves to, if any."""
        schema = self.schema if schema is None else schema
        resolved = schema.resolve_column(column, self.open_tables(doc, schema=schema))
        if resolved is not None and resolved.tables:
            return resolved.tables[0]
        pooled = schema.column_by_name(column)
        if pooled is not None and pooled.tables:
            return pooled.tables[0]
        return None

    # -- building sources (the add-source modal's engine edge) --------------

    def next_source_id(self, hint: str) -> str:
        """A registry id for a new source, kept unique against the live registry."""
        return _source_id(hint, self.sources)

    def build_sqlite_source(self, path: str, display: str = "") -> DataSource:
        """Build (not register) a local D1/SQLite snapshot source."""
        hint = display or Path(path).stem
        return add_sqlite_source(self.next_source_id(hint or "snapshot"), path, display or None)

    def build_live_source(
        self,
        account_id: str,
        api_token: str,
        database: str,
        display: str = "",
    ) -> DataSource:
        """Build (not register) a live Cloudflare D1 source over its HTTPS API."""
        hint = display or database or account_id
        return add_d1_live_source(
            self.next_source_id(hint),
            account_id=account_id,
            api_token=api_token,
            database=database,
            display=display or None,
        )

    def build_postgres_source(
        self, *, host: str, database: str, user: str, password: str,
        port: int = 5432, schema: str = "public", sslmode: str = "prefer", display: str | None = None,
    ) -> DataSource:
        """Build a real PostgreSQL source; Test does not register or persist it."""
        hint = display or database or "postgres-live"
        if password:
            hint = hint.replace(password, "redacted")
        return add_postgres_source(
            self.next_source_id(hint),
            host=host, port=port, database=database, user=user, password=password,
            schema=schema, sslmode=sslmode, display=display,
        )

    # -- statements the query cannot carry ----------------------------------

    def _supports(self, name: str) -> bool:
        """Whether the active source advertises that capability."""
        return bool(capabilities_for(self.source).get("supports", {}).get(name, False))

    def tx_state(self) -> TxState:
        """The active source's transaction state, created on first use."""
        return self.tx.setdefault(self.active_id, TxState())

    def temp_tables(self) -> list[str]:
        """The temp tables the active source holds — what `\\drop` can drop."""
        return [name for name, entry in self.source.datasets.items() if entry.get("temp")]

    def apply_tx(self, kind: str, name: str | None) -> str:
        """Run one transaction command and report it, or raise for a user mistake.

        The transaction lives on the connection (so a later run stays inside
        it); this keeps the session's own view of it — open or not, and which
        savepoints exist — in step, including SQL's rule that `ROLLBACK TO`
        keeps its savepoint while `RELEASE` (and everything after it) is gone.
        """
        if not self._supports("transactions"):
            raise PayloadError(
                "a live D1 source has no transactions — it is reached over HTTP, "
                "so every query stands alone"
            )
        state = self.tx_state()
        con = self.source.con
        if kind == "begin":
            if state.open:
                raise PayloadError(
                    f"a transaction is already open on {self.active_id} — \\commit or \\rollback first"
                )
            begin(con)
            state.open = True
            return f"transaction open · {self.active_id}"
        if kind in ("commit", "rollback"):
            if not state.open:
                raise PayloadError(f"no transaction is open on {self.active_id} — \\begin first")
            if kind == "commit":
                commit(con)
                message = f"transaction committed · {self.active_id}"
            else:
                rollback(con)
                message = f"transaction rolled back · {self.active_id}"
            state.open = False
            state.savepoints.clear()
            if kind == "rollback":
                self._prune_temp()
            return message
        if not state.open:
            raise PayloadError(f"no transaction is open on {self.active_id} — \\begin first")
        if kind == "savepoint":
            if name in state.savepoints:
                raise PayloadError(f'savepoint "{name}" already exists')
            savepoint(con, name or "")
            state.savepoints.append(name or "")
            return f'savepoint "{name}" set'
        if name not in state.savepoints:
            listed = ", ".join(state.savepoints) or "(none)"
            raise PayloadError(f'unknown savepoint "{name}" — open: {listed}')
        kept = state.savepoints[: state.savepoints.index(name)]
        if kind == "release":
            release(con, name or "")
            state.savepoints = kept
            return f'savepoint "{name}" released'
        rollback_to(con, name or "")
        state.savepoints = [*kept, name]
        self._prune_temp()
        return f'rolled back to savepoint "{name}"'

    def _prune_temp(self) -> None:
        """Re-derive the temp tables the engine still holds after a rollback.

        Temp-table DDL is transactional on both engines, so a rollback takes a
        `\\temp` created inside the transaction (or after the savepoint) with it
        — and puts back a `\\drop` that undid one. The registry follows both
        ways, or the explorer would keep offering a table that is gone and hide
        one that is there.
        """
        dropped = self._dropped.get(self.active_id, {})
        if not any(entry.get("temp") for entry in self.source.datasets.values()) and not dropped:
            return
        try:
            if self.source.kind == "postgres-live":
                namespace = self.source.con._session_temp_db
                live = set(self.source.con.list_tables(database=(self.source.postgres["database"], namespace))) if namespace else set()
            else:
                live = set(self.source.con.list_tables())
        except Exception:  # a source that cannot list locally keeps what it has
            return
        changed = False
        for name in [
            name
            for name, entry in self.source.datasets.items()
            if entry.get("temp") and name not in live
        ]:
            del self.source.datasets[name]
            changed = True
        for name, entry in [
            (name, entry)
            for name, entry in dropped.items()
            if name in live and name not in self.source.datasets
        ]:
            self.source.datasets[name] = entry
            del dropped[name]
            changed = True
        for name in [name for name in dropped if name not in live]:
            del dropped[name]
        if changed:
            self.refresh_schema()

    def tables(self) -> dict:
        """The active source's datasets as resolved expressions.

        Handing them to the engine keeps it from asking the connection for a
        table by name: SQLite reads a table's schema inside a transaction of its
        own, which would commit the session's open one.
        """
        return {name: entry["table"] for name, entry in self.source.datasets.items()}

    def materialize_temp(
        self,
        name: str,
        payload: dict,
        dialect: str,
        schema,
        tables: dict | None = None,
    ) -> dict:
        """Run the payload and keep its rows as a temp table on the active source.

        Only a name that is not already the session's — a dataset, or this
        document's own CTE — is refused; re-running a `\temp` replaces the table
        it made last time, which is what an IDE's run key should do.
        """
        if not self._supports("temp"):
            raise PayloadError(
                "a live D1 source cannot hold a temp table — it is reached over HTTP"
            )
        entry = self.source.datasets.get(name)
        if entry is not None and not entry.get("temp"):
            raise PayloadError(f'"{name}" is already a dataset — pick another temp name')
        if name in {cte["name"] for cte in payload.get("ctes") or []}:
            raise PayloadError(f'"{name}" is a CTE in this document — pick another temp name')
        result = materialize(self.source.con, payload, name, dialect, tables=tables)
        self.source.datasets[name] = {
            "table": temp_handle(self.source.con, name, schema),
            "doc": "temp table · created this session",
            "rows": len(result["rows"]),
            "temp": True,
        }
        # A fresh table means fresh values: nothing cached for this source still holds.
        self._values = {key: value for key, value in self._values.items() if key[0] != self.active_id}
        self.refresh_schema()
        return result

    def drop_temp_table(self, name: str) -> None:
        """Drop a temp table this session created — `\\drop` is not general DDL."""
        entry = self.source.datasets.get(name)
        if entry is None or not entry.get("temp"):
            raise PayloadError(f'"{name}" is not a temp table')
        drop_temp(self.source.con, name)
        del self.source.datasets[name]
        # A drop inside a transaction can be rolled back, which puts the table
        # back on the engine: what it was — its handle, its row count — is kept
        # so the registry can follow.
        self._dropped.setdefault(self.active_id, {})[name] = entry
        self._values = {key: value for key, value in self._values.items() if key[0] != self.active_id}
        self.refresh_schema()

    # -- running ------------------------------------------------------------

    def run(self, doc: str, record: bool = True) -> RunOutcome:
        """Parse, run the document's statements, then its query; never raises.

        A document is a statement: its transaction commands run first, in the
        order they were written, then `\\drop`, and then the query — which is
        the one thing `\\temp` wraps (`CREATE TEMP TABLE … AS`). A document of
        statements alone runs them and answers with their status; the rows a
        query produced are the rows it asked for, capped for the pane.
        """
        try:
            if not self.source_connected():
                raise PayloadError("The saved target is disconnected. Reconnect it or explicitly select another source before running.")
            schema = self.schema
            ast = parse_query(doc, schema=schema, settled=True)
            if ast.errors:
                first = ast.errors[0]
                message = f"line {first.line}: {first.message}"
                return RunOutcome(status=f"not executed · {message}", error=message)
            payload = self._query_payload(ast)
            queried = has_query(ast)
            steps: list[str] = []
            dialect = dialect_for(self.source, self.dialect)
            for step in ast.tx:
                steps.append(self.apply_tx(step.kind, step.name))
            if ast.drop:
                self.drop_temp_table(ast.drop.name)
                steps.append(f'dropped temp table "{ast.drop.name}"')
            if not queried:
                if ast.temp:
                    raise PayloadError(
                        "\\temp needs a query to keep — this document has no \\from"
                    )
                return self._statement_outcome(steps, dialect, doc, dropped=bool(ast.drop), record=record)
            tables = self.tables()
            expr = build(self.source.con, payload, tables=tables)
            if ast.temp:
                result = self.materialize_temp(
                    ast.temp.name, payload, dialect, expr.schema(), tables
                )
                steps.append(f'created temp table "{ast.temp.name}"')
            elif self.source.d1 is not None:
                result = execute_remote(
                    self.source.d1, self.source.con, payload, dialect, tables=tables
                )
            else:
                result = execute(self.source.con, payload, dialect, tables=tables)
        except (PayloadError, D1Error) as exc:
            return RunOutcome(status=str(exc), error=str(exc))
        except Exception as exc:  # last-resort guard: a UI may not crash or exit
            message = f"{type(exc).__name__}: {exc}"
            return RunOutcome(status=message, error=message)

        returned = result["rows"]
        shown = returned[:PREVIEW_ROW_CAP]
        total = len(returned)
        if payload.get("limit") is None and len(shown) < total:
            counted = self.count_rows(expr)
            if counted is not None and counted > total:
                total = counted
        dtypes = self.dtypes_of(expr, len(result["columns"]))
        dialect = result["dialect"] or self.dialect
        outcome = RunOutcome(
            ok=True,
            source=self.active_id,
            dialect=dialect,
            columns=list(result["columns"]),
            dtypes=dtypes,
            rows=shown,
            total=total,
            ms=result["ms"],
            sql=result["sql"],
            schema_changed=bool(ast.temp or ast.drop),
        )
        outcome.status = " · ".join([*steps, self._status_for(outcome)])
        if record:
            # A library preview is not a run the user made; it stays out of history.
            self._record_history(
                HistoryEntry(
                    at=time.strftime("%Y-%m-%d %H:%M:%S%z"),
                    source=self.active_id,
                    dialect=dialect,
                    rows=total,
                    ms=result["ms"],
                    doc=doc,
                    target=self.source_key(),
                ),
                outcome,
            )
        return outcome

    def _statement_outcome(
        self, steps: list[str], dialect: str, doc: str, dropped: bool = False, record: bool = True,
    ) -> RunOutcome:
        """A run that carried statements and no query: their status, no rows."""
        outcome = RunOutcome(
            ok=True,
            source=self.active_id,
            dialect=dialect,
            status=" · ".join(steps) or "nothing to run",
            schema_changed=dropped,
        )
        if record:
            self._record_history(
                HistoryEntry(
                    at=time.strftime("%Y-%m-%d %H:%M:%S%z"),
                    source=self.active_id,
                    dialect=dialect,
                    rows=0,
                    ms=0.0,
                    doc=doc,
                    target=self.source_key(),
                ),
                outcome,
            )
        return outcome

    def compile(self, doc: str) -> tuple[str | None, str]:
        """(sql, message): the document rendered for the active dialect.

        Compiling never executes; `sql` is None when the document or the dialect
        is refused, and `message` is then the user-facing reason. The statements
        a document may carry — transactions, `\\temp`, `\\drop` — are not SQL to
        render, so they are named in the message instead.
        """
        try:
            if not self.source_connected():
                raise PayloadError("The saved target is disconnected. Reconnect it or explicitly select another source before compiling.")
            schema = self.schema
            ast = parse_query(doc, schema=schema, settled=True)
            if ast.errors:
                first = ast.errors[0]
                return None, f"line {first.line}: {first.message}"
            payload = self._query_payload(ast)
            if not has_query(ast):
                return None, "nothing to compile — this document carries statements only"
            dialect = dialect_for(self.source, self.dialect)
            sql = compile_sql(build(self.source.con, payload, tables=self.tables()), dialect)
        except (PayloadError, D1Error) as exc:
            return None, str(exc)
        except Exception as exc:  # last-resort guard: a UI may not crash or exit
            return None, f"{type(exc).__name__}: {exc}"
        note = " · \\temp materializes on run" if ast.temp else ""
        return sql, f"compiled for {dialect} · {len(sql.splitlines())} lines{note}"

    def count_rows(self, expr) -> int | None:
        """The document's full row count, for the preview-cap note.

        Runs the engine's own `count()` over the built expression; on a source
        that cannot answer locally (a live D1's unbound tables) it reports
        nothing and the caller falls back to what it actually read.
        """
        try:
            counted = expr.count().execute()
        except Exception:
            return None
        try:
            # An ungrouped count comes back as a scalar; a grouped one as a
            # frame with one count per group, whose sum is the row count.
            if hasattr(counted, "iloc"):
                return int(sum(int(value) for value in counted.iloc[:, 0] if value is not None))
            return int(counted)
        except (TypeError, ValueError, IndexError, AttributeError):
            return None

    @staticmethod
    def dtypes_of(expr, width: int) -> list[str]:
        """The result's dtype labels, aligned with the executed column list."""
        try:
            labels = [type_name(dtype) for _, dtype in expr.schema().items()]
        except Exception:
            labels = []
        if len(labels) != width:
            return [""] * width
        return labels

    @staticmethod
    def _status_for(outcome: RunOutcome) -> str:
        status = f"{len(outcome.rows)} rows · {outcome.ms:.1f} ms · {outcome.source} · {outcome.dialect}"
        if outcome.capped:
            status += f" · showing {len(outcome.rows)} of {outcome.total} (preview capped)"
        return status
