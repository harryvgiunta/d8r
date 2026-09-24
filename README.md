# anyQL

**A keyboard-first IDE for querying data — one document, any datasource, any
dialect, right in your terminal.**

anyQL is a real application, not a mockup. It is a pure-Python
[Textual](https://textual.textualize.io) TUI: what you type is a document in a
short `\command` language, that document is parsed into an AST, the AST builds a
real [ibis](https://ibis-project.org) expression, and the rows you see come from
real query execution on an in-process DuckDB — or, if you add one, on your own
Cloudflare D1 database. There is no server to start, no browser, and no port to
open. `uv run anyql` is the whole app.

## The model

Three axes, orthogonal by design:

```
document  →  datasource (which schema it points at)  →  dialect (what SQL it becomes)
    │                          │                                │
    └──────────── compile (never executes) ──────────────────────┘
                               └──────────── run ───────────────┘
```

- **The document is the source of truth.** A short clause language (`\from`,
  `\join`, `\select`, `\where`, `\group`, `\order`, `\case`, `\limit`, `\with`)
  parses to a pure AST. No hand-written SQL anywhere in the stack.
- **The datasource is the schema context.** Pick one and the schema tree,
  completion, and validation all follow it. The bundled registry holds a live
  demo source and four vendor-shaped mock connections; sources you add yourself
  join the same picker.
- **The dialect is a rendering choice.** Any compilable target can render any
  document against any datasource — that is what the SQL tab shows, and it runs
  nothing. 20 targets are advertised; 19 compile here (PySpark needs a live
  session config, so it is listed as unavailable).

## Install and run

```bash
uv sync          # create/refresh the project environment (.venv)
uv run anyql     # launch the IDE
```

Dependencies are declared in `pyproject.toml` and locked in `uv.lock`; nothing
is installed globally, and every command runs in the project's `.venv`. Python
`>=3.13` is required (pinned in `.python-version`). `uv run python -m anyql` is
the equivalent module form.

A fresh session opens on the `demo` source with a real query already in the
editor, so `ctrl+enter` does something true on the very first keystroke.

## The screen

```
┌ anyQL   [ Local demo · DuckDB + Parquet ▾ ]  [ DuckDB ▾ ]  duckdb ───────────┐
│ Schema              │ Document                                               │
│  events  100 rows   │  \from events                                          │
│   ▸ timestamp       │  \select event_type                                    │
│   ▸ user_id         │  \select sum(amount) as total                          │
│  users  25 rows     │  \group event_type                                     │
│                     ├─ Results │ SQL │ History ──────────────────────────────┤
│                     │  event_type · string          total · float64          │
│                     │  click                        …                        │
└─────────────────────────────────────────────────────────────────────────────┘
  ctrl+enter run · ctrl+k compile · ctrl+o data source · \ palette · tab panes · ctrl+q quit
```

- **Header** — the datasource picker, the dialect picker, and a pill naming the
  backend actually in use (`duckdb`, `postgres (mock)`, `sqlite (D1 snapshot)`,
  `sqlite (Cloudflare D1)`). Switching either picker re-points the whole app:
  the tree, the parser's schema seam, and the palette's offers.
- **Schema pane** — the active source's datasets with their row counts. Expand a
  dataset to load its columns (name + dtype) and select one to insert its name
  into the document.
- **Document pane** — the editor (line numbers on), with the `\` palette
  floating under the caret.
- **Results / SQL / History tabs** — Results shows the executed rows, or the
  parser/engine error line when a run is refused; SQL shows the compiled
  document for the active dialect (read-only, never executed); History lists
  every document this session has run; selecting a row loads that document back
  into the editor.
- **Footer** — the key hints, and a status line that reports what just happened
  (rows, milliseconds, source, dialect).

## Keys

| Keys | Effect |
| --- | --- |
| `ctrl+enter` (or `f5`) | Run: execute the document, render Results, append History |
| `ctrl+k` (or `f6`) | Compile: render the document as SQL for the active dialect, execute nothing |
| `ctrl+o` | Add a data source |
| `\` | Open the command palette at the caret |
| `up` / `down` | Move the palette highlight (with the palette closed: move the caret) |
| `enter` | In the document: accept the highlighted suggestion — or insert a newline when there is nothing to accept. In a table: select the row under the cursor |
| `escape` | Close the palette, or cancel the add-source form |
| `shift+left` / `shift+right` | Narrow / widen the results column under the cell cursor |
| arrows, `page up`/`page down`, `home`/`end` | Move around the results and history tables |
| `tab` / `shift+tab` | Move focus between the panes |
| `ctrl+q` | Quit |

## The `\` palette

Type `\` anywhere in the document. The palette appears under the caret and
follows what you type:

- On a bare `\` it lists the clause commands — `\from`, `\open`, `\join`,
  `\select`, `\where`, `\group`, `\order`, `\case`, `\limit`, `\with` — plus the
  app's actions: **Run**, **Compile**, **Data source…**, **History**. Typing a
  prefix filters the list; Enter completes the command in place and leaves a
  space for its argument.
- After a command it offers that command's arguments: dataset names for
  `\from`/`\open`/`\join`, column names (with their dtype and dataset) for
  `\select`/`\group`/`\order`, and for `\where` a three-step chain — column,
  then operator, then that column's distinct values. Numeric values are inserted
  bare, text values quoted (`\where event_type = "purchase"`).
- `enter` on the highlight accepts it; if the highlight is already exactly what
  you typed, Enter simply starts a new line. `escape` closes the palette without
  touching the document, and clicking a row accepts it.
- The offers stay on as you type and after you accept one: an accept lands them
  where the same characters typed by hand would have, so `\select` keeps
  offering fields and `sum(` keeps offering its argument. They go quiet by
  themselves where the language has nothing left to say — `\from events ` takes
  an alias rather than a second table, and one `\where` sets one value — so
  Enter is a newline again as soon as the clause is done. Moving the caret with
  the arrows never summons the palette back; the next keystroke does.
- Columns offered after `\from` come from the open tables of *this* document;
  with no `\from` yet, the palette offers the registry's cross-dataset pool.

`\case`, `\limit`, and `\with` take arguments you type yourself — the palette has
no suggestions for them.

## Datasources and dialects

The bundled registry (all local, all deterministic):

| Source | What it is |
| --- | --- |
| `Local demo · DuckDB + Parquet` | 100 web events and 25 users on a real in-process DuckDB |
| `Acme Commerce · PostgreSQL (mock)` | orders, customers |
| `Catalog DB · MySQL (mock)` | products, reviews |
| `Marketing DW · Snowflake (mock)` | campaigns, spend_log |
| `Web Analytics · BigQuery (mock)` | web_sessions, conversions |

The four vendor connections are exactly that: **mock connections**. Each carries
a declared dialect, a vendor-shaped schema, and deterministic generated data,
and each executes through the same in-process DuckDB engine as the demo. There
is no network behind them and no credentials. They exist to exercise the seam a
real connection would use — and, together with the dialect picker, to make the
point of the product: write one document, then read it back as any vendor's SQL.

## Add a Cloudflare D1 database

`ctrl+o` opens the credential form. Two shapes, one form:

- **Live** — fill in **Account ID** (the 32-character Cloudflare account id),
  **Database** (name or uuid), and **API token**. `Test connection` introspects
  the database and reports what it holds — `D1 · orders-db · 3 tables · users,
  orders` — before anything is registered; `Add` registers it and re-points the
  app at it.
- **Local snapshot** — put a path to a `.sqlite` file exported from D1
  (`wrangler d1 execute --local`) in **Local snapshot**. That skips the network
  entirely, and the same form serves an offline user. An explicit path is
  required; nothing is opened implicitly.

Live sources ship their compiled SQLite SQL to Cloudflare's D1 REST API and run
on the real engine there; snapshots run on a real SQLite engine in-process. The
**API token** you paste is used per request and kept only in memory: it is never
written to disk, never logged, and never echoed back into the UI.

## The results explorer

- The results table is a spreadsheet-style explorer: a **cell cursor** moves with
  the arrow keys (and page/home/end), and the executed dtypes ride along in the
  column headers (`user_id · int64`).
- `shift+left` / `shift+right` **resize the column** under the cursor.
- `NULL` values render as `NULL` rather than as blank cells.
- The table buffers the **first 10 000 rows** of a result. When a document
  returns more than that, the status line says so — `showing 10000 of 12000
  (preview capped)` — while the run itself still executed the document exactly
  as written.
- A refused run never crashes the app: the parser's message (with its line
  number) appears on the Results tab, and the footer repeats it as the status.

## The language

```anyql
\from events
\select event_type
\select sum(amount) as total
\group event_type
\order total desc
\limit 10
```

Commands are case-insensitive, lists need no commas, and values with spaces are
written as wrapped strings. The full contract — every command and argument rule,
star projections, `over ( … )` window frames, `\case`, table aliases, the exact
error strings — lives in **[docs/AST.md](docs/AST.md)**.

## Tests

```bash
uv run pytest -q                          # 170 tests: the language/engine contract + the TUI end to end
uv run python verify/check_canonical.py   # the language gate — prints MATCH
```

The TUI tests drive the real app headlessly with Textual's pilot
(`App.run_test()`) against the real engine, so no browser, no server, and no
terminal are needed to check a change.

## Layout

```
anyql/
  __main__.py   `uv run python -m anyql`
  query/        pure language layer (parser, AST, alias rules, schema registry)
  engine/       ibis/DuckDB execution, datasource registry, dialects, D1 client,
                deterministic demo data, make_data.py
  tui/          the app: widgets, session core, `\` palette, add-source modal, styles
tests/          pytest suite + checked-in fixtures
spec/           canonical document ↔ AST pair (the language contract)
docs/AST.md     the language contract
verify/         check_canonical.py — the language gate
```
