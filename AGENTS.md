# D8R — agent guide

## What this repo is

D8R is the **actual application**: a keyboard-first **IDE for manipulating
data through Ibis**, running entirely in this terminal as a
[Textual](https://textual.textualize.io) TUI. It is a code-IDE interaction model
(one document, a `\` command palette, contextual completion, results/SQL/history
panes) pointed at data instead of at another SQL workbench.

The **document** — a short `\command` language — is the source of truth.
Everything derives from it: the AST is always recomputed from the text (nothing
caches it across edits), the AST becomes a payload, the payload builds a real
ibis expression, and the widgets render whatever comes back. Nothing writes
back into the document except user edits, palette inserts, explicit user-requested
AI query updates, and proposals accepted with Apply or Auto accept AI updates.

Three layers, one direction:

- **Language** (`d8r/query/`) — `parse_query(doc, schema=...)` is a pure
  function of document text and an immutable schema context; `payload_from_ast`
  maps the AST to the engine payload.
- **Engine** (`d8r/engine/`) — in-process [ibis](https://ibis-project.org)
  over DuckDB: payload → expression (`build`) → SQL (`compile_sql`) → rows
  (`execute`). No SQL is hand-written anywhere.
- **TUI** (`d8r/tui/`) — Textual widgets that *consume* the two layers above.
  `Session` (`d8r/tui/session.py`) is the headless core; `app.py` wires it to
  widgets; `palette.py` decides what `\` offers.

## Hard boundaries (never add these)

| Off-limits | Do instead |
| --- | --- |
| Real database connections | DuckDB in-process over the Parquet under `d8r/engine/data/` |
| An HTTP server or a browser UI | **The TUI is the app.** `uv run d8r`; nothing listens on a port |
| Authentication | No login, no users, no session identity |
| Credentials handling | Only the user-authorized D1, PostgreSQL and AI-provider credentials below; save locally as authorized, never log or echo them unmasked |
| Persistence | Only custom functions, saved connection credentials, app/AI settings, and the user-authorized workspace/history state below persist; runtime database/results state stays in memory |
| Hand-written SQL generation | Compose ibis expressions; render with `ibis.to_sql(expr, dialect=…)` |

If a task seems to require one of these, it is out of scope. Build the *UI
affordance* for it, not the feature.

**User-authorized exception — Cloudflare D1.** Pointing D8R at a real D1
database was an explicit, deliberate override of the rows above, so it is scoped
exactly as narrowly as it is implemented today and nothing else may lean on it:
`kind="d1-live"` reaches Cloudflare's official D1 REST API
(`d8r/engine/d1api.py`) using credentials the *user* pastes into the
add-source modal, and `kind="d1"` opens a local `.sqlite` snapshot of a D1
database through ibis's SQLite backend, entirely in-process. There is still no
login/user/session concept. Credentials persist only through the local storage exception below.
The live path keeps the architecture intact: payloads build against *unbound*
ibis tables carrying the D1 database's real schemas and compile to SQLite SQL
exactly like every other source; only the execution seam differs — the compiled
SQLite SQL is POSTed to D1's `/query`|`/raw` endpoints instead of run on a local
connection (`execute_remote` in `d8r/engine/execute.py`). The client uses the
token per request; explicit **Add** saves it locally for reuse. Tokens are never
logged or rendered unmasked (the modal's API-token input is `password=True`,
and the built source never renders it). Startup reconnects only the selected saved source.
Live D1 connects with a lightweight authenticated probe, not full schema discovery.
Run fetches only missing metadata for referenced tables through the existing Ibis
path; a separate background index fills the explorer and completion snapshot.
A small "Indexing schema…" indicator never reserves the query connection lane.
Index failures/timeouts leave Run available and do not overwrite results/history;
late index results only update their original registered source, never a replacement
or a closed app. Partial schema snapshots permit not-yet-indexed table references.
Compile stays offline, using only already-loaded metadata. Neither path counts
table/view rows; the explorer reports "row count not loaded".

**User-authorized exception — PostgreSQL.** The user explicitly requested a real
PostgreSQL connection and a local Docker database. `kind="postgres-live"` uses
Ibis's PostgreSQL backend with psycopg, through the existing payload → expression
→ execute path. The add-source modal accepts host/port/database/user/password,
schema and SSL mode. It discovers accessible user schemas' tables/views together
without row-count scans or extension installation. The selected schema remains
the default for bare table names; qualified and quoted schema prefixes complete
across schemas without network I/O while typing. Test/Cancel save nothing; Add
saves PostgreSQL profiles and passwords in `memory.json`. Passwords are masked,
preserved verbatim and never logged. Saved targets restore disconnected; explicit
selection or workspace Run reconnects. Run reuses saved PostgreSQL/D1 credentials
or a SQLite snapshot path through compact nonmodal connection progress, then executes the captured
document only if the target identity still matches. Failure or Cancel executes
nothing; changing the target requires a fresh Run. Startup reconnects the selected
saved target after Mount, without executing its document or opening a credentials modal.
Saved connections have a 30-second deadline; blinking dots and Cancel replace
the loading bar. Cancel/timeout/target switches revoke the attempt and
dispose late-built sources. Missing credentials still require the connection form.
Connection workers start after Mount dispatch (`call_later`), never directly inside
`on_mount`: Textual still reports `is_mounted=False` during that event, which can
otherwise abandon the attempt before either connecting or starting its timeout.
Transactions/savepoints/temp tables share one
autocommit connection with explicit transaction ownership; temp DDL/handles are
qualified to `pg_temp`, never persistent tables. Production roles should be
least-privileged and use provider-appropriate TLS. `compose.yaml` provisions an
isolated loopback-only PostgreSQL development service on port 55433 with a named
volume; its password lives in gitignored `.env.postgres`. This authorizes no
other database/network backend, server, login system or arbitrary SQL execution.

**User-authorized exception — local configuration.** `d8r/storage.py` stores
custom functions (name, ordered parameters, body, description) and D1 profiles
(account ID, resolved database UUID, display label, API token) in `memory.json`.
`settings.json` stores Intellisense, the optional entry screen (off by default),
pane visibility, selected source/dialect, default returned rows, and AI provider URL/model/API key/tool-round/call/sample
limits/attempts/timeout. Configuration files are editable JSON under
`~/.d8r` on every platform; `D8R_DATA_DIR` overrides that home, and explicit
`Session(data_dir=...)` wins. Settings displays the resolved settings path.
These files and backups contain plaintext secrets: protect them. New directories
and files use owner-only POSIX modes; Windows relies on filesystem ACLs.
`Session` loads configuration without network or schema-dependent function-body validation.
Function Save/Run preview and Delete persist definitions immediately, as do
explicit user-requested workspace AI saves through `save_function` and manual
**Save function** on preview proposals. Query/function-form AI Apply and ordinary
edits autosave drafts only. D1 Add saves credentials; Test and Cancel do not.
Saved profiles fill the masked token; legacy token-free profiles still load.
Toggle preferences save on change; AI provider and default-row forms require explicit Save.
Import only fills the form. Unavailable workspace targets restore as disconnected,
never silently redirected to another source. No automatic directory migration:
copy existing files or point D8R_DATA_DIR at their directory. Atomic replacement
and stale-session detection protect all local files; a lock or
temp file left by a crashed save clears itself after a minute. Invalid/unreadable
files remain untouched, surface a safe UI error, and block saves to that file
until recovery and restart. External file edits require restart. Tests isolate
the home per test. Runtime result buffers, transactions, and temporary tables never persist.

**User-authorized exception — durable workspace and history.** `workspace.json`
autosaves named query pages (document/caret/stable identity/source/dialect), the
active document and Schema/Pages explorer tab, last workspace/function view,
unsaved function draft and target, successful query history, and independently
identified, target-scoped AI chats/composer drafts/statuses. `\pages`/`\queries`
reveal or toggle Pages; page switches save drafts without connecting or executing. Source
references include local snapshot paths; credentials remain in their existing
settings/memory files. Chats can contain schema, sampled data and sensitive drafts;
workspace files and backups are plaintext. Debounced edits flush latest widget
contents at exit. Session-owned writes merge under an RLock and reuse atomic
storage/stale-writer protection. A failed autosave retains drafts/results in memory
and reports the error; it never relabels an already executed query as failed.
Session restoration builds disconnected placeholders without network or snapshot opening.
After Mount, the app reconnects only the selected source using saved credentials/path
and cancellable progress. Startup never executes a query, requests AI, Applies, or Saves
a function definition. Missing credentials remain disconnected. Completed tool exchanges
are validated before replay; partial replies,
diagnostics and applicable proposals are not restored. New chat preserves earlier
conversations. App-owned requests survive chat switches and function-screen closure;
app exit cancels them, and startup never resumes them. Agents (`ctrl+j`) uses an
oh-my-pi-style status-first roster with counts, coloured markers and muted target/activity details:
Busy means running; Ready means a new chat or an unread completed reply; Seen means a read completed
reply. Error and Cancelled remain distinct, with unread failures marked separately. Highlighting
does not mark a reply read. Returning from the function editor refreshes current IntelliSense
without overriding the user's disabled/dismissed state.

**Contextual table and join completion.** Selecting a table through IntelliSense or
an active explorer table-argument offer inserts a short collision-free alias (`projects p`,
then `p2`, `p3` if needed). Column offers and explorer insertions use the document's
alias; ordinary typed text is not rewritten. Declared PostgreSQL, SQLite and D1
foreign keys supply qualified `on` suggestions in both directions, including every
pair of composite keys. Completion reads immutable metadata only, never the network.
Successful history ranks matching commands, tables, columns, functions and cached
values by recency within the current source identity; match quality and field/function
groups still lead. History aliases are rebound to the current document's identifiers.

**Default returned rows.** `Session.default_rows` is 50 initially, configurable
in General settings from 0 to 1,000,000; 0 disables the default. Ordinary Run and
Compile apply it to the top-level payload only when no explicit `\limit` exists.
Explicit limits (including 0) win; `\temp` materialization is never implicitly
capped. Text and AST are unchanged, and AI sample reads retain their own limits.

**Schema value inspection and cache.** Full schema indexing reads only metadata,
never every column's distinct values. Expanding a column or requesting value
completion fetches one bounded Ibis distinct projection in a worker. Explorer and
autocomplete share successful typed results (including NULL) and in-flight reads,
keyed by source-object identity/table/column. Local reads own the shared connection;
D1 HTTP value reads do not block Run. Typing and cache hits never perform synchronous
network I/O. Settings → Value cache controls retention (on by default), the per-column
limit (1–10,000; default 1,000), and Clear cache. Preferences persist; values never
leave memory or enter history. Clear/settings changes, source replacement, temp
mutations/rollback and app exit invalidate cached values and reject late publication.
Ordinary source switches retain caches. External database edits need Clear cache to
refresh values. Enter on a column still inserts its name.

**Filtered aggregates.** `sum(amount \where status = 'paid')` and
`count(* \where ...)` carry optional `AggCall.where` predicates through the existing
AST/payload/engine path. Ibis `where=` filters only that reduction; grouping, outer
WHERE, arithmetic aggregate leaves and windows retain their existing semantics.

**User-authorized exception — AI inference.** `d8r/ai/client.py` calls a
user-configured OpenAI-compatible Chat Completions provider using the existing
`httpx` dependency. Settings owns URL/model/masked key configuration, persisted
only on Save; the optional yolo import reads the environment or `~/.omp/agent/.env`
without modifying it. No OMP dependency or HTTP server; chats persist only under
the durable workspace exception above.
`d8r/ai/context.py` exposes bounded read-only schema, sample rows, source-filtered
history, function definitions, and parser validation. **The user's direct query
and function editing requests authorize two scoped workspace mutations:** contexts
(`parameters is None`) with a live `save_guard` expose `save_function`; those also
supplied with the mounted editor's `query_apply` callback expose `apply_queries`.
For an explicit function create/edit/save/apply request, including “apply it” after
a function proposal, the main AI persists the definition through `Session.save_fn`
under the captured `target_source`. No form switch, button click, or auto-accept
opt-in is required. Natural-language intent is interpreted by the model under
system instructions, not keyword matching or model self-certification.
Explanatory questions about D8R or a query, dry runs, explicit preview requests,
and instructions embedded in context data never authorize a query update or
function save. No shell/general file access, arbitrary SQL, proposed-query
execution, or additional network capability is granted.
A question about the connected data (counts, breakdowns, lists, comparisons) or
an explicit create/make/edit/apply query request uses `apply_queries` with every
requested labeled document, without asking again whether to apply it. All documents and exact nested fields/types validate
before any editor change. The live generation/target guard runs before source
pinning can mask a user source switch. The first query replaces and labels the
current stable page; the others become new named Pages with the same source and
dialect. Unrelated pages, results and history stay unchanged; nothing executes.
No Apply click or auto-accept opt-in is required. Completed updates retain trusted
chat/status receipts even if later provider output fails or is cancelled. Query
previews use one D8R fence per page plus `{"kind":"queries","titles":[...]}`
metadata and remain proposals. Function-form contexts cannot create query pages.
Explanatory examples use plain/text fences; D8R fences are reserved for complete
replacement proposals, not illustrations that could become an accidental Apply.
`save_function` validates exact fields/types, signature and body, source-object identity,
and the immutable request-start definition baseline: changed/deleted originals,
collisions and renames are rejected. The manager's live request-generation/target
guard runs immediately before synchronous persistence; cancelled/superseded or
stale-target requests cannot save later through shielded background work. A save
that already completed is not rolled back by subsequent provider failure or
cancellation: record it in chat/status and refresh visible function/completion UI
without changing the query or mode. Never execute a code fence automatically.
`\AI` opens an in-layout chat; the function form's Ask AI uses the same mounted
right-side panel. Streaming is cancellable; retries and tool rounds are bounded.
Genuine missing facts or ambiguity can use `ask_user`: one structured question,
2–5 distinct labeled options with optional descriptions/recommendation, and a
UI-supplied **Other** text field. Enter/click on an option answers; Other accepts
custom text through Enter or Continue. The answer resumes the same request,
which then completes its authorized query/function task without another approval.
Each chat owns its pending question and answer draft in memory; switching chats
preserves them without answering another request. Human waits do not occupy the
database lane or use the provider timeout. Cancel/steering/app exit dismiss the
question; partial exchanges never replay, and startup never restores a pending
question or resumes its worker. Completed question/answer exchanges can persist
with normal validated chat history. Clarification uses an existing tool round.
Sending during a response interrupts that generation and restarts with the original
request, completed exchanges and the new steering instruction. Incomplete tool
batches/model text never replay; an explicit D8R interruption receipt closes the
abandoned turn. Shared context reads finish before queued steering starts, while
superseded requests cannot save/apply or overwrite the new worker's state.
Incomplete replies never become applicable proposals. Manual Apply or the
explicit, persisted Auto accept AI updates preference (off by default) can change
the document/body after parser validation and an exact stale-target check. Neither
draft-Apply path executes queries or saves function definitions; auto-accept is
unrelated to direct query updates or function saves. Requests for previews retain
workspace function proposals with manual **Save function**; function-form AI stays
draft-only. Auto-accept applies
only a completed draft response to a still-matching mounted editor; restored history
never auto-applies. Schema, sample rows, history, and submitted drafts may be sent
to the configured provider.
Function editing is not a modal: `FnExplorer` replaces the schema tree in the left
slot, `FnEditor` replaces the query in the middle, preview uses the bottom slot,
and `#workspace-ai` remains on the right. `PaneSplitter` mouse/arrow resizing
persists `pane_sizes` (explorer/ai widths and editor height in terminal cells) in
settings. Mode switches retain widgets/drafts and sizes; viewport clamping never
overwrites preferred dimensions.

## Architecture rules

- **Pure core, thin edges.** `parse_query(doc, schema=...)` is a pure function
  of text and an immutable context; the AST carries no state (see `docs/AST.md`). The engine is pure
  payload → ibis; the TUI is a consumer of both and re-implements no rule of
  either.
- **Data flows one way:** document text → AST → payload → ibis expression →
  rows → widgets. Views never mutate the AST; results are *inputs to UI state*,
  never written back into the document.
- **No network in the UI.** No TUI module imports a server framework, and
  importing the app loads none — `tests/test_tui.py::test_the_tui_reaches_no_http_server`
  enforces exactly that, structurally and at import time. Network edges are the
  live-D1 client inside the engine and the AI-provider client in `d8r/ai/`.
- **Schema state lives at one seam.** `d8r/query/schema.py` defines the immutable
  `SchemaContext` the parser's validation and palette both read.
  `Session.refresh_schema()` replaces its session-owned snapshot of tables,
  capabilities, column pool, and functions on source/schema/function changes.
  Each parse/completion captures one context; no process-global registry exists.
- **Capability-driven completion.** What `\` offers filters against
  `capabilities_for(source)` — aggregates, operators, window functions, dtype
  families. A backend that advertises differently changes what the IDE offers.
- **Types over `any`; no dead code.** User mistakes are values, not crashes:
  the parser reports errors on the AST, the engine raises `PayloadError` /
  `D1Error`, and the app renders both as text. `Session.run` has a last-resort
  guard because a UI must never die on a keystroke.

## The datasource registry

`d8r/engine/datasources.py` owns it; `load()` builds it with no arguments
(paths resolve from the engine's own directory, never the working directory):

- **`demo`** — the bundled Parquet under `d8r/engine/data/` (`events` 100
  rows, `users` 25 rows) on a real in-process DuckDB. Default dialect `duckdb`.
- **Four vendor mocks** — `postgres` (orders, customers), `mysql` (products,
  reviews), `snowflake` (campaigns, spend_log), `bigquery` (web_sessions,
  conversions). A mock connection is *declared dialect + vendor-shaped schema +
  deterministic data* and executes on the same in-process DuckDB as the demo;
  nothing more. `capabilities_for` labels each one honestly
  (`"<dialect> (mock)"`).
- **Cloudflare D1 sources, added at runtime** (live connections are never bundled or restored automatically; profiles and API tokens are saved locally):
  `add_sqlite_source` (`kind="d1"`, a local `.sqlite` snapshot, opened
  in-process; `sqlite_*`/`_cf_*` objects are skipped, the same filter D1's own
  console applies) and `add_d1_live_source` (`kind="d1-live"`, real schemas
  pulled into unbound ibis tables via `schema_connection`, SQL shipped over the
  REST API with the token rule above). Both default to the `sqlite` dialect.

`DIALECTS` advertises 20 compile targets — DuckDB, Athena, BigQuery,
ClickHouse, Databricks, DataFusion, Druid, Exasol, Flink, Impala, Materialize,
SQL Server, MySQL, Oracle, PostgreSQL, PySpark, RisingWave, Snowflake, SQLite,
Trino. 19 compile; `pyspark` is listed as known-but-uncompilable (`compiles:
False`) and the dialect picker labels it `· unavailable`. The dialect is an
independent rendering choice: any document can be compiled for any target, and
`compile` never executes.

## Layout

```
d8r/
  __main__.py     `uv run python -m d8r` — opens the IDE in this terminal
  query/          pure language layer: parser.py (grammar + errors + payload +
                  block/clause lookup), ast.py (types + to_json), alias.py
                  (derived output names), schema.py (the registry seam)
  engine/         datasources.py (registry, dialects, capabilities, D1 sources),
                  expression.py (payload → ibis), execute.py (compile + execute,
                  execute_remote for live D1, materialize for `\temp`),
                  tx.py (transactions, savepoints, temp tables),
                  d1api.py (Cloudflare REST client),
                  make_data.py (deterministic Parquet + fixtures),
                  data/ (demo Parquet + per-mock mirrors), d1/ (the shipped
                  D1 snapshot)
  tui/            app.py (widgets + bindings, pane visibility), session.py
                  (headless core, run, compile, history, PREVIEW_ROW_CAP,
                  shared column-value cache), palette.py (`\` rules, matching, offers),
                  settings.py (the `\settings` menu), fn.py (the `\fn` function
                  library + editor), add_source.py (the
                  `ctrl+o` modal), splash.py (the 6-second boot animation),
                  app.tcss
tests/            pytest suite (630: 564 query/engine + 66 TUI) + fixtures/
spec/             canonical-query.d8r ↔ canonical-query.ast.json (the contract)
docs/AST.md       language contract — read before touching d8r/query/
verify/           check_canonical.py — the language gate
pyproject.toml    project metadata + pinned deps (runtime: ibis-framework[duckdb],
                  pyarrow, pyarrow-hotfix, httpx, textual; dev group: pytest),
                  and the `d8r` console script (`d8r.tui.app:main`)
uv.lock           the resolved lockfile — `uv sync` installs exactly this
.python-version   the interpreter pin (Python >=3.13)
```

## Commands

Everything below is offline and binds no port. There is no dev server to
start, in this repo or next to it. Nothing is installed globally: every
command runs in the project's `.venv`.

```bash
uv sync                                      # create/refresh the project environment (.venv)
uv run d8r                                 # launch the IDE in this terminal
uv run pytest -q                             # the whole suite (630 tests, 6 skipped)
uv run python verify/check_canonical.py      # language contract gate → prints MATCH
uv run python -m d8r.engine.make_data      # regenerate demo Parquet + fixtures
```

Dependencies are declared in `pyproject.toml` (runtime pins plus a `pytest` dev
group) and locked in `uv.lock` with `uv lock`. A new dependency means editing
`pyproject.toml` and re-locking — `pyproject.toml` plus the lockfile are the
only dependency records, and there is no second requirements file.

## Verification bar
- `uv run pytest -q` must pass — 630 tests (624 pass, 6 skipped): the query/engine contract
  (parser regimes, payload mapping, datasources, D1 sources, set operations,
  subqueries, lateral joins, window frames, regex, table-valued functions, temp
  tables and transactions, execute vs fixtures) plus the TUI end to end.
- `uv run python verify/check_canonical.py` must print `MATCH` — deep equality of
  `parse_query(canonical)` against `spec/canonical-query.ast.json`.
- **UI changes are verified headlessly with Textual's pilot**
  (`App.run_test()`, exactly as `tests/test_tui.py` does): real widgets against
  the real engine, no terminal and no network required. There is no browser, and
  no read-only state bridge into one any more — that projection died with the
  deleted front end.
- Do not add linters, formatters, CI, or browser automation. Do not add a
  server, a socket, or a second execution path.

## Data conventions

- Datasets are deterministic Parquet under `d8r/engine/data/` — demo files at
  the root (`events.parquet`, `users.parquet`), mock mirrors under
  `data/<datasource-id>/`. Pure arithmetic, zero randomness: regenerating with
  `uv run python -m d8r.engine.make_data` produces byte-identical files.
- Add a demo dataset by dropping a Parquet file into `d8r/engine/data/`
  (optionally with a `CATALOG` entry in `datasources.py` for its description);
  add a mock by extending `MOCKS` in `datasources.py` — `load()` calls
  `ensure_mock_data`, so missing mirrors are written on demand. Discovery,
  schema, capabilities, completion, and the tree all pick both up.
- Checked-in JSON fixtures under `tests/fixtures/` are deep-equality targets:
  `expected_schema.json`, `expected_rows.json`, `expected_mock_rows.json` (all
  three written by `make_data.py`) plus the window/case/CTE/join/alias/set-op
  row fixtures.
- **The canonical fixture pair is the language contract:**
  `spec/canonical-query.d8r` ↔ `spec/canonical-query.ast.json`. Parser
  semantics changes update both files and `docs/AST.md` together, and must keep
  `verify/check_canonical.py` printing `MATCH`.
- The shipped D1 snapshot (`d8r/engine/d1/d1.sqlite`) contains only synthetic
  `stations` (3 rows) and `readings` (12 rows). `make_data.py` builds a fresh
  SQLite file and atomically replaces the destination, never opening or copying
  an old snapshot. Never bundle production exports, PII, or authentication data.
  It is opened only when a caller names it — never a hidden default.

## Keybindings (copied from the code — do not invent)

App-level, `d8r/tui/app.py:223-232`:

| Keys | Action | Source |
| --- | --- | --- |
| `ctrl+enter` | Run: parse, execute, render Results, append History | `app.py:224` |
| `f5` | Run (hidden alias) | `app.py:225` |
| `ctrl+k` | Compile: render SQL for the active dialect, execute nothing | `app.py:226` |
| `f6` | Compile (hidden alias) | `app.py:227` |
| `ctrl+o` | Add a data source (the D1 modal) | `app.py:228` |
| `ctrl+comma` | Settings — the full-screen menu, and the way back when the Intellisense switch has the palette shut | `app.py:231` |
| `ctrl+j` | Agents — active requests, unread replies and saved chat history | `D8RApp.BINDINGS` |

While the focus is in the document pane, `d8r/tui/app.py:62-71`:

| Keys | Action | Source |
| --- | --- | --- |
| `up` / `down` | Move the palette highlight; with it closed, move the caret | `app.py:63-64` |
| `enter` | Accept the highlighted suggestion; otherwise insert a newline | `app.py:65` |
| `escape` | Show/hide the popup (nothing else in the document) | `app.py:66` |
| `ctrl+a` | Select the whole document (TextArea would go to line start) | `app.py:69` |
| `tab` | Accept the highlighted suggestion — it never moves focus | `app.py:70` |

Results explorer, `d8r/tui/app.py:158-160`:

| Keys | Action | Source |
| --- | --- | --- |
| `shift+left` / `shift+right` | Narrow / widen the column under the cell cursor | `app.py:159-160` |

Add-source modal, `d8r/tui/add_source.py:27`: `escape` cancels (registers
nothing). Settings menu, `d8r/tui/settings.py:90`: `escape` backs out one
level, and leaves Settings from the root. Function library (`\fn`),
`d8r/tui/fn.py`: `ctrl+r` previews the selected function (its grid shows the
call's rows, and a preview never lands in History), `ctrl+d` deletes it, `ctrl+q`
backs out, and `ctrl+c` copies selected text. Escape toggles body completion.
Tab accepts an offer, otherwise moves to the next field. Clicking a palette row accepts it,
like Enter on the highlight (`app.py` `_palette_clicked`).
Beyond the keys, the `\` palette carries the workspace actions: `\results`,
`\sql`, `\history` and `\schema` show/hide their pane — accepting the row, so
nothing is ever written into the document — `\settings` opens the full-screen
menu, the one place every setting lives (Show/Hide Menus for all four panes,
Intellisense, Keybindings read live off the widget classes, Data source, Dialect),
and `\fn` opens the function library (see fn.py). Accepting a clause command instead takes
its own line, or the line that clause already has in the block being edited:
`\from`, `\open`, `\join`, `\union`, `\intersect`, `\except`, `\select`,
`\distinct`/`\unique`, `\where`, `\group`, `\order`, `\case`, `\limit`, `\with`, `\temp`, `\drop`,
`\begin`, `\savepoint`, `\release`, `\rollback`, `\commit`.

The app's nonpriority `ctrl+q` returns to the query in function mode; elsewhere it
quits. Results/history tables use `DataTable`'s own keys (arrows, Enter, page/home/end).
`IdeScreen` consumes Tab/Shift+Tab in query mode and navigates fields in function
mode; modals keep Textual's field navigation. The footer names the active mode's keys.

## Working notes (verified the hard way)

- **Git history was rewritten to remove a production snapshot.** Do not merge
  pre-purge history or restore the old database from another clone or backup.
  `.venv/`, `__pycache__/`, and `.pytest_cache/` are gitignored; only synthetic
  Parquet and SQLite fixtures belong in the repository.
- **Numeric projections use Ibis arithmetic.** `\select total_cents / 100 as
  total_dollars` and `sum(amount_cents) / 100` perform numeric conversion, including
  non-truncating division on SQLite. The language supports binary `+ - * /`, unary
  signs and parentheses; zero divisors become NULL through Ibis `nullif(0)`.
  Arithmetic has its own AST/payload node, not SQL strings or Python eval. Numeric
  types/NULL only; aggregate-only math uses the aggregate path, row math groups
  implicitly, constants do not group. Mixed aggregate/row dependencies in one
  expression require a CTE. Scalar arguments can contain row math, but aggregates
  still take columns (or `count(*)`). Window/subquery results need a CTE before math.
  Expression trees are bounded to 32 levels and 256 nodes in parser and engine.
- **Parser validation has two regimes.** Unknown-table, qualified-prefix, and
  CTE-shadows-dataset errors fire **only when the supplied context has tables**.
  Duplicate table identifiers, duplicate CTE names, nested CTEs, and every
  grammar error fire regardless. The canonical fixture uses the default empty
  context; strict-prefix cases pass a loaded `SchemaContext` explicitly.
- **The last non-empty line is the typing line**: half-typed commands there are
  normal and are *not* errors — that line may still be growing. Everything
  before it reports. Explicit Run/Compile pass `settled=True` to validate every line.
- **Parser work is bounded.** Numeric tokens are limited to 640 characters.
  Function expansion rejects active-stack cycles and limits each parse to 16
  nested calls and 256 total calls. Save validates the candidate definition
  before persisting; legacy cyclic definitions remain editable but cannot crash
  parsing. Command boundaries are fixed before substituting balanced arguments.
- **The palette's keys belong to `EditorPane`, not to the `TextArea`** — a
  TextArea's own key handler cannot be suppressed from a subclass, so the
  bindings are claimed one level up and stay scoped to the pane. That is what
  keeps `enter` selecting rows in the results table.
- **The results table is a preview buffer**: `Session.run` keeps the first
  `PREVIEW_ROW_CAP = 10000` rows and leaves the rest unread, while the status
  line reports `showing N of M (preview capped)`. This buffer cap is independent
  of the user-configured default query limit; it never rewrites a query.
- **Results and SQL are tabs, not containers.** `\results`/`\sql` drive
  `TabbedContent.hide_tab`/`show_tab` (public API in textual 8.2.7; hiding the
  active tab makes Textual activate the next shown one), `\schema` sets
  `#schema-pane.display`, and `App._show_tab` leaves a hidden pane hidden — a
  run never forces its pane back into view.
- **Intellisense is on unless Escape says otherwise.** The popup follows the
  caret wherever the language has something to offer — typed to, inserted into
  from the schema tree, or **accepted** into: an accept re-syncs where the same
  characters typed by hand would have landed, so `\sel`, `sum(` and the clause
  a jump lands on keep offering instead of needing a second summon. A bare
  caret move only carries an open popup along; summoning one on every arrow key
  would put the popup in front of the document's own navigation. Escape closes
  the popup and keeps it shut while typing continues — a fresh `\` still opens
  the command list, since a keystroke does not hide the language — and a second
  Escape brings the offers back. The Intellisense switch silences everything, so
  `ctrl+comma` opens Settings without the palette.
- **`tab` accepts; no key walks the panes.** `IdeScreen` (`app.py:127`) binds
  `tab`/`shift+tab` to a no-op: inherited `Screen` bindings survive a subclass's
  `BINDINGS`, so the keys are re-bound rather than dropped, and a claimed key is
  consumed before the focused `TextArea`'s own `tab` handler (`tab_behavior`
  `"focus"`) can fire. `EditorPane`'s priority `tab` wins inside the document
  and accepts the suggestion. Modals keep Textual's tab-between-fields.
- **`ctrl+a` selects the whole document.** `TextArea` binds `home,ctrl+a` to
  line start, so `EditorPane` claims the key for the same reason.
- **A clause command takes its own line — or the line its clause has.**
  Accepting `\where` (or any clause command) asks `clause_line`
  (`d8r/query/parser.py:286`) for that clause in the block the caret is
  editing: found, the caret goes there and the document is untouched; not found,
  the command breaks the line first. `\open` is `\from`, and `\unique` is
  `\distinct`; a repeated clause answers with its last line; a `\with` body
  is a block of its own.
- **Distinct compares output rows.** `\distinct` / `\unique` take no arguments,
  use Ibis `distinct()` after projection and set operations, and precede ordering
  and limits. Each CTE, inline subquery, and function body owns its own flag.
- **Function parameters complete from the draft.** In the `\fn` body editor,
  `@` offers the current form's declared names, including unsaved edits. These
  offers respect parameter quote/boundary rules and never leak to the workspace.
- **A screen must not define `_render`.** `Widget._render` is Textual's own hook
  returning the visual a widget paints; naming the settings menu's redraw
  `_render` shadowed it and killed rendering for the whole screen (`AttributeError:
  'NoneType' object has no attribute 'render_strips'`). That method is `_draw`.
- Timestamps leave the engine as ISO strings; NaN/None become `None` (rendered
  as `NULL` in the table).
- **A document is one statement.** Transaction commands run in document order
  *before* the query, then `\drop`, then the query — with `\temp` materializing
  what that query produced — so `\begin` + `\temp t` in one run is "open a
  transaction, then keep this"; a document of statements alone runs them and
  answers with their status, rows-free. Transaction state is per source, lives
  in `Session.tx`, and the transaction itself lives on the connection.
- **Inline subqueries are the same grammar on one line.** `( \from … \select … )`
  is parsed by `parse_subquery` into a full `QueryAST` whose every node reports
  the *clause's* line (`ParseOpts.fixed_line`) because that is the only line it
  has; errors bubble to the document and the nested `errors` list is cleared,
  exactly like a `\with` body. A parenthesized `\where` operand is a subquery
  only when it contains a `\`, so `\where amount = (3)` still compares to `(3)`.
- **LATERAL is correlation hoisting.** ibis has no correlated subquery inside a
  join, so `\join lateral` lifts the body's `\where` equality against an outer
  column into the join predicate, and a body `\limit` becomes the top-N rewrite
  (`row_number()` over the correlation's partition, filtered, helper column
  dropped). Anything that is not an equality correlation is refused by name —
  never approximated — and a body table may not reuse a name from the left, or
  every reference to it would be ambiguous (`duplicate table identifier`). The
  body's ordering columns are re-projected under `_d8r_order_N` names first:
  a window frame may only depend on one relation.
- **`\temp` is `CREATE TEMPORARY TABLE … AS`** around ibis' own rendering of the
  query, and the rows the pane shows are read back *from the table*. It is the
  one write in the app, so it is what a transaction has to show for itself:
  temp-table DDL is transactional on DuckDB and SQLite alike, so `\begin` in one
  run, `\temp t` in the next, then `\rollback` leaves no `t`. `Session._prune_temp`
  re-derives *which* temp tables there are from the connection after every
  rollback — removing what the transaction took and putting back what its DDL
  undid (a `\drop` is stashed in `Session._dropped` for exactly that). A
  restored table's handle is name-based (`tx.temp_handle`), so it reads the
  table the name resolves to now, never a snapshot of a dropped one. (All of a document's
  directives run before its query, so a `\begin` and a `\rollback` in the *same*
  document cannot wrap the table it keeps.)
  Temp DDL explicitly targets SQLite's `temp` schema or DuckDB's `temp.main`
  namespace, so replacement and drop never fall back to persistent tables.
  `tx.create_temp` refuses `sqlite_` and `_cf_` prefixes case-insensitively,
  before compilation or any database mutation.
- **ibis' `create_table`/`drop_table` are unusable in this build.** With the
  locked sqlglot (30.19), `sge.Drop(...).sql("duckdb")` renders `DROP TABLE IF
  EXISTS` with **no name**, so `drop_table` (and every `overwrite=True` path)
  raises a parser error; SQLite's `create_table` additionally wraps itself in
  `self.begin()`, which would commit the session's own transaction. Hence
  `d8r/engine/tx.py` issues `DROP`/`CREATE TEMPORARY TABLE … AS` through the
  backend's own handle.
- **`con.table(name)` commits an open SQLite transaction.** ibis' SQLite
  `get_schema` runs inside its own transaction, so introspection ends the
  session's; that is why `Session.tables()` hands the registry's already-resolved
  handles to `build`/`execute`/`materialize` (the `tables` parameter) and why a
  temp table's handle is built from its schema (`tx.temp_handle`) instead of
  being looked up. DuckDB is unaffected, but the handles save its introspection
  too.
- **A frame needs the ordering it counts from.** `rows|range between …` inside
  `over ( … )` without `order by` is refused with its own message (a frame is
  counted from the ordering); bounds are canonical text in the AST whatever case
  and spacing they were typed with.
- **`~`/`!~` are predicates, not filters on strings only.** A non-string column
  is named as the mistake (`needs a string column`), and the palette stops
  offering values after `~`, `!~`, `in` and `not in` — their operand is typed.
  It also reads the capability flags: `\savepoint`/`\release` are offered only
  where `supports.savepoints` says the engine has them (SQLite does, DuckDB does
  not), exactly as the operator list comes from the backend.
