# anyQL — agent guide

## What this repo is

anyQL is the **actual application**: a keyboard-first **IDE for manipulating
data through Ibis**, running entirely in this terminal as a
[Textual](https://textual.textualize.io) TUI. It is a code-IDE interaction model
(one document, a `\` command palette, contextual completion, results/SQL/history
panes) pointed at data instead of at another SQL workbench.

The **document** — a short `\command` language — is the source of truth.
Everything derives from it: the AST is always recomputed from the text (nothing
caches it across edits), the AST becomes a payload, the payload builds a real
ibis expression, and the widgets render whatever comes back. Nothing writes
back into the document except the user and the palette's own inserts.

Three layers, one direction:

- **Language** (`anyql/query/`) — `parse_query(doc)` is a pure function of the
  document text; `payload_from_ast` maps the AST to the engine payload.
- **Engine** (`anyql/engine/`) — in-process [ibis](https://ibis-project.org)
  over DuckDB: payload → expression (`build`) → SQL (`compile_sql`) → rows
  (`execute`). No SQL is hand-written anywhere.
- **TUI** (`anyql/tui/`) — Textual widgets that *consume* the two layers above.
  `Session` (`anyql/tui/session.py`) is the headless core; `app.py` wires it to
  widgets; `palette.py` decides what `\` offers.

## Hard boundaries (never add these)

| Off-limits | Do instead |
| --- | --- |
| Real database connections | DuckDB in-process over the Parquet under `anyql/engine/data/` |
| An HTTP server or a browser UI | **The TUI is the app.** `uv run anyql`; nothing listens on a port |
| Authentication | No login, no users, no session identity |
| Credentials handling | Nothing to authenticate against; never introduce secrets beyond the D1 token below |
| Persistence | State lives in memory (active source, dialect, history, added sources); it dies with the process |
| Hand-written SQL generation | Compose ibis expressions; render with `ibis.to_sql(expr, dialect=…)` |

If a task seems to require one of these, it is out of scope. Build the *UI
affordance* for it, not the feature.

**One user-authorized exception — Cloudflare D1.** Pointing anyQL at a real D1
database was an explicit, deliberate override of the rows above, so it is scoped
exactly as narrowly as it is implemented today and nothing else may lean on it:
`kind="d1-live"` reaches Cloudflare's official D1 REST API
(`anyql/engine/d1api.py`) using credentials the *user* pastes into the
add-source modal, and `kind="d1"` opens a local `.sqlite` snapshot of a D1
database through ibis's SQLite backend, entirely in-process. There is still no
login/user/session concept, no persisted secret, and no other remote service.
The live path keeps the architecture intact: payloads build against *unbound*
ibis tables carrying the D1 database's real schemas and compile to SQLite SQL
exactly like every other source; only the execution seam differs — the compiled
SQLite SQL is POSTed to D1's `/query`|`/raw` endpoints instead of run on a local
connection (`execute_remote` in `anyql/engine/execute.py`). **The token is
request-scoped: it lives only in the in-memory source, is used per query, and is
never persisted, logged, or echoed back into the UI** (the modal's API-token
input is `password=True` and the built source never renders it).

## Architecture rules

- **Pure core, thin edges.** `parse_query(doc)` is a pure function of document
  text; the AST carries no state (see `docs/AST.md`). The engine is pure
  payload → ibis; the TUI is a consumer of both and re-implements no rule of
  either.
- **Data flows one way:** document text → AST → payload → ibis expression →
  rows → widgets. Views never mutate the AST; results are *inputs to UI state*,
  never written back into the document.
- **No network in the UI.** No TUI module imports a server framework, and
  importing the app loads none — `tests/test_tui.py::test_the_tui_reaches_no_http_server`
  enforces exactly that, structurally and at import time. The single network
  edge in the whole repo is the live-D1 client inside the engine.
- **Schema state lives at one seam.** `anyql/query/schema.py` is the registry
  the parser's validation and the palette both read; `Session.refresh_schema()`
  installs the active source's tables + capabilities through
  `set_schema_state(...)`, and re-installs them on every source change (never
  cached across sources).
- **Capability-driven completion.** What `\` offers filters against
  `capabilities_for(source)` — aggregates, operators, window functions, dtype
  families. A backend that advertises differently changes what the IDE offers.
- **Types over `any`; no dead code.** User mistakes are values, not crashes:
  the parser reports errors on the AST, the engine raises `PayloadError` /
  `D1Error`, and the app renders both as text. `Session.run` has a last-resort
  guard because a UI must never die on a keystroke.

## The datasource registry

`anyql/engine/datasources.py` owns it; `load()` builds it with no arguments
(paths resolve from the engine's own directory, never the working directory):

- **`demo`** — the bundled Parquet under `anyql/engine/data/` (`events` 100
  rows, `users` 25 rows) on a real in-process DuckDB. Default dialect `duckdb`.
- **Four vendor mocks** — `postgres` (orders, customers), `mysql` (products,
  reviews), `snowflake` (campaigns, spend_log), `bigquery` (web_sessions,
  conversions). A mock connection is *declared dialect + vendor-shaped schema +
  deterministic data* and executes on the same in-process DuckDB as the demo;
  nothing more. `capabilities_for` labels each one honestly
  (`"<dialect> (mock)"`).
- **Cloudflare D1 sources, added at runtime** (never persisted, never bundled):
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
anyql/
  __main__.py     `uv run python -m anyql` — opens the IDE in this terminal
  query/          pure language layer: parser.py (grammar + errors + payload +
                  block/clause lookup), ast.py (types + to_json), alias.py
                  (derived output names), schema.py (the registry seam)
  engine/         datasources.py (registry, dialects, capabilities, D1 sources),
                  expression.py (payload → ibis), execute.py (compile + execute,
                  execute_remote for live D1), d1api.py (Cloudflare REST client),
                  make_data.py (deterministic Parquet + fixtures),
                  data/ (demo Parquet + per-mock mirrors), d1/ (the shipped
                  D1 snapshot)
  tui/            app.py (widgets + bindings, pane visibility), session.py
                  (headless core, run, compile, history, PREVIEW_ROW_CAP,
                  VALUE_POOL_LIMIT), palette.py (`\` rules, matching, offers),
                  settings.py (the `\settings` menu), add_source.py (the
                  `ctrl+o` modal), app.tcss
tests/            pytest suite (186: 155 query/engine + 31 TUI) + fixtures/
spec/             canonical-query.anyql ↔ canonical-query.ast.json (the contract)
docs/AST.md       language contract — read before touching anyql/query/
verify/           check_canonical.py — the language gate
pyproject.toml    project metadata + pinned deps (runtime: ibis-framework[duckdb],
                  pyarrow, pyarrow-hotfix, httpx, textual; dev group: pytest),
                  and the `anyql` console script (`anyql.tui.app:main`)
uv.lock           the resolved lockfile — `uv sync` installs exactly this
.python-version   the interpreter pin (Python >=3.13)
```

## Commands

Everything below is offline and binds no port. There is no dev server to
start, in this repo or next to it. Nothing is installed globally: every
command runs in the project's `.venv`.

```bash
uv sync                                      # create/refresh the project environment (.venv)
uv run anyql                                 # launch the IDE in this terminal
uv run python -m anyql                       # the same entry point, module form
uv run pytest -q                             # the whole suite (170 tests)
uv run python verify/check_canonical.py      # language contract gate → prints MATCH
uv run python -m anyql.engine.make_data      # regenerate demo Parquet + fixtures
```

Dependencies are declared in `pyproject.toml` (runtime pins plus a `pytest` dev
group) and locked in `uv.lock` with `uv lock`. A new dependency means editing
`pyproject.toml` and re-locking — `pyproject.toml` plus the lockfile are the
only dependency records, and there is no second requirements file.

## Verification bar

- `uv run pytest -q` must pass — 186 tests: the query/engine contract
  (parser regimes, payload mapping, datasources, D1 sources, execute vs
  fixtures) plus the TUI end to end.
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

- Datasets are deterministic Parquet under `anyql/engine/data/` — demo files at
  the root (`events.parquet`, `users.parquet`), mock mirrors under
  `data/<datasource-id>/`. Pure arithmetic, zero randomness: regenerating with
  `uv run python -m anyql.engine.make_data` produces byte-identical files.
- Add a demo dataset by dropping a Parquet file into `anyql/engine/data/`
  (optionally with a `CATALOG` entry in `datasources.py` for its description);
  add a mock by extending `MOCKS` in `datasources.py` — `load()` calls
  `ensure_mock_data`, so missing mirrors are written on demand. Discovery,
  schema, capabilities, completion, and the tree all pick both up.
- Checked-in JSON fixtures under `tests/fixtures/` are deep-equality targets:
  `expected_schema.json`, `expected_rows.json`, `expected_mock_rows.json` (all
  three written by `make_data.py`) plus the window/case/CTE/join/alias row
  fixtures.
- **The canonical fixture pair is the language contract:**
  `spec/canonical-query.anyql` ↔ `spec/canonical-query.ast.json`. Parser
  semantics changes update both files and `docs/AST.md` together, and must keep
  `verify/check_canonical.py` printing `MATCH`.
- The shipped D1 snapshot (`anyql/engine/d1/d1.sqlite`) is a real database
  opened only when a caller names it — never a hidden default.

## Keybindings (copied from the code — do not invent)

App-level, `anyql/tui/app.py:223-232`:

| Keys | Action | Source |
| --- | --- | --- |
| `ctrl+enter` | Run: parse, execute, render Results, append History | `app.py:224` |
| `f5` | Run (hidden alias) | `app.py:225` |
| `ctrl+k` | Compile: render SQL for the active dialect, execute nothing | `app.py:226` |
| `f6` | Compile (hidden alias) | `app.py:227` |
| `ctrl+o` | Add a data source (the D1 modal) | `app.py:228` |
| `ctrl+comma` | Settings — the full-screen menu, and the way back when the Intellisense switch has the palette shut | `app.py:231` |

While the focus is in the document pane, `anyql/tui/app.py:62-71`:

| Keys | Action | Source |
| --- | --- | --- |
| `up` / `down` | Move the palette highlight; with it closed, move the caret | `app.py:63-64` |
| `enter` | Accept the highlighted suggestion; otherwise insert a newline | `app.py:65` |
| `escape` | Show/hide the popup (nothing else in the document) | `app.py:66` |
| `ctrl+a` | Select the whole document (TextArea would go to line start) | `app.py:69` |
| `tab` | Accept the highlighted suggestion — it never moves focus | `app.py:70` |

Results explorer, `anyql/tui/app.py:158-160`:

| Keys | Action | Source |
| --- | --- | --- |
| `shift+left` / `shift+right` | Narrow / widen the column under the cell cursor | `app.py:159-160` |

Add-source modal, `anyql/tui/add_source.py:27`: `escape` cancels (registers
nothing). Settings menu, `anyql/tui/settings.py:90`: `escape` backs out one
level, and leaves Settings from the root. Clicking a palette row accepts it,
like Enter on the highlight (`app.py` `_palette_clicked`).

Beyond the keys, the `\` palette carries the workspace actions: `\results`,
`\sql`, `\history` and `\schema` show/hide their pane — accepting the row, so
nothing is ever written into the document — and `\settings` opens the
full-screen menu, the one place every setting lives: Show/Hide Menus (all four
panes), Intellisense, Keybindings (read live off the widget classes), Data
source, and Dialect. Accepting one of the ten clause commands instead takes its
own line, or the line that clause already has in the block being edited.

Provided by Textual, not by this app: `ctrl+q` quits (`textual/app.py`,
`App.BINDINGS`), and the results/history tables use `DataTable`'s own keys
(arrows for the cell cursor, `enter`, page/home/end for scrolling). The IDE's
own screen (`IdeScreen`, `app.py:127`) re-binds `tab`/`shift+tab` to a no-op, so
nothing walks the panes; the modals keep Textual's tab-between-fields. The
footer's hint line (`KEY_HINTS`, `app.py:47`) names the app's own keys.

## Working notes (verified the hard way)

- **Not a git repo**: no diff or `git status` safety net; enumerate touched
  files deliberately, and no `git` verification commands.
- **Parser validation has two regimes.** Unknown-table, qualified-prefix, and
  CTE-shadows-dataset errors fire **only when the schema registry is non-empty**
  (`set_schema_state`). Duplicate table identifiers, duplicate CTE names, nested
  CTEs, and every grammar error fire regardless. A harness for the canonical
  fixture therefore needs an *empty* registry; the strict-prefix cases need a
  loaded one — `tests/test_query.py` has both fixtures.
- **The last non-empty line is the typing line**: half-typed commands there are
  normal and are *not* errors — that line may still be growing. Everything
  before it reports.
- **The palette's keys belong to `EditorPane`, not to the `TextArea`** — a
  TextArea's own key handler cannot be suppressed from a subclass, so the
  bindings are claimed one level up and stay scoped to the pane. That is what
  keeps `enter` selecting rows in the results table.
- **The results table is a preview buffer**: `Session.run` keeps the first
  `PREVIEW_ROW_CAP = 10000` rows and leaves the rest unread, while the status
  line reports `showing N of M (preview capped)`. The engine still runs the
  document exactly as written — the cap is not a query rewrite.
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
  Accepting `\where` (any of the ten) asks `clause_line`
  (`anyql/query/parser.py:286`) for that clause in the block the caret is
  editing: found, the caret goes there and the document is untouched; not found,
  the command breaks the line first. `\open` is `\from`; a repeated clause
  answers with its last line; a `\with` body is a block of its own.
- **A screen must not define `_render`.** `Widget._render` is Textual's own hook
  returning the visual a widget paints; naming the settings menu's redraw
  `_render` shadowed it and killed rendering for the whole screen (`AttributeError:
  'NoneType' object has no attribute 'render_strips'`). That method is `_draw`.
- Timestamps leave the engine as ISO strings; NaN/None become `None` (rendered
  as `NULL` in the table).
