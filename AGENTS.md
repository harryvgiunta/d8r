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
back into the document except user edits, palette inserts, and an AI proposal
the user explicitly accepts with Apply.

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
| Credentials handling | Only the user-authorized D1 and AI-provider keys below; save locally as authorized, never log or echo them unmasked |
| Persistence | Only custom functions, D1 profiles/tokens, app/AI settings, and the user-authorized workspace/history state below persist; runtime database/results state stays in memory |
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
and the built source never renders it). Startup never connects automatically.

**User-authorized exception — local configuration.** `d8r/storage.py` stores
custom functions (name, ordered parameters, body, description) and D1 profiles
(account ID, resolved database UUID, display label, API token) in `memory.json`.
`settings.json` stores Intellisense, pane visibility, selected source/dialect,
default returned rows, and AI provider URL/model/API key/tool-round/call/sample
limits/attempts/timeout. Configuration files are editable JSON under
`~/.d8r` on every platform; `D8R_DATA_DIR` overrides that home, and explicit
`Session(data_dir=...)` wins. Settings displays the resolved settings path.
These files and backups contain plaintext secrets: protect them. New directories
and files use owner-only POSIX modes; Windows relies on filesystem ACLs.
`Session` loads configuration without network or schema-dependent function-body validation.
Function Save/Run preview and Delete persist definitions immediately; AI Apply
and ordinary edits autosave drafts only. D1 Add saves credentials; Test and Cancel do not.
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
autosaves the document/caret/stable identity, selected source/dialect, last
workspace/function view, unsaved function draft and target, successful query
history, and per-target completed AI conversations/composer drafts. Source
references include local snapshot paths; credentials remain in their existing
settings/memory files. Chats can contain schema, sampled data and sensitive drafts;
workspace files and backups are plaintext. Debounced edits flush latest widget
contents at exit. Session-owned writes merge under an RLock and reuse atomic
storage/stale-writer protection. A failed autosave retains drafts/results in memory
and reports the error; it never relabels an already executed query as failed.
Startup restores unavailable sources as disconnected placeholders without any
network, snapshot opening, query execution, AI request, Apply, or function-definition
Save. Completed tool exchanges are validated before replay; partial replies,
diagnostics and applicable proposals are not restored. New chat clears only its
target. Returning from the function editor refreshes current IntelliSense without
overriding the user's disabled/dismissed state.

**Default returned rows.** `Session.default_rows` is 50 initially, configurable
in General settings from 0 to 1,000,000; 0 disables the default. Ordinary Run and
Compile apply it to the top-level payload only when no explicit `\limit` exists.
Explicit limits (including 0) win; `\temp` materialization is never implicitly
capped. Text and AST are unchanged, and AI sample reads retain their own limits.

**User-authorized exception — AI inference.** `d8r/ai/client.py` calls a
user-configured OpenAI-compatible Chat Completions provider using the existing
`httpx` dependency. Settings owns URL/model/masked key configuration, persisted
only on Save; the optional yolo import reads the environment or `~/.omp/agent/.env`
without modifying it. No OMP dependency or HTTP server; chats persist only under
the durable workspace exception above.
`d8r/ai/context.py` exposes bounded read-only schema, sample rows, source-filtered
history, function definitions, and parser validation; it grants no shell/file or
arbitrary query-execution tool. `\AI` opens an in-layout chat; the function form's
Make with AI uses the same panel. Streaming is cancellable, retries and tool
rounds are bounded, and incomplete replies never become applicable proposals.
Only explicit Apply changes the document/body, after parser validation and a
stale-target check; Apply never executes a query or saves a function. Schema,
sample rows, history, and submitted drafts may be sent to the configured provider.

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
                  VALUE_POOL_LIMIT), palette.py (`\` rules, matching, offers),
                  settings.py (the `\settings` menu), fn.py (the `\fn` function
                  library + editor), add_source.py (the
                  `ctrl+o` modal), app.tcss
tests/            pytest suite (298: 255 query/engine + 43 TUI) + fixtures/
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
uv run pytest -q                             # the whole suite (298 tests)
uv run python verify/check_canonical.py      # language contract gate → prints MATCH
uv run python -m d8r.engine.make_data      # regenerate demo Parquet + fixtures
```

Dependencies are declared in `pyproject.toml` (runtime pins plus a `pytest` dev
group) and locked in `uv.lock` with `uv lock`. A new dependency means editing
`pyproject.toml` and re-locking — `pyproject.toml` plus the lockfile are the
only dependency records, and there is no second requirements file.

## Verification bar
- `uv run pytest -q` must pass — 298 tests: the query/engine contract
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
call's rows, and a preview never lands in History), `ctrl+d` deletes it, `escape`
back out. Clicking a palette row accepts it,
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

Provided by Textual, not by this app: `ctrl+q` quits (`textual/app.py`,
`App.BINDINGS`), and the results/history tables use `DataTable`'s own keys
(arrows for the cell cursor, `enter`, page/home/end for scrolling). The IDE's
own screen (`IdeScreen`, `app.py:127`) re-binds `tab`/`shift+tab` to a no-op, so
nothing walks the panes; the modals keep Textual's tab-between-fields. The
footer's hint line (`KEY_HINTS`, `app.py:47`) names the app's own keys.

## Working notes (verified the hard way)

- **Git history was rewritten to remove a production snapshot.** Do not merge
  pre-purge history or restore the old database from another clone or backup.
  `.venv/`, `__pycache__/`, and `.pytest_cache/` are gitignored; only synthetic
  Parquet and SQLite fixtures belong in the repository.
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
