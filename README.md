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
  `\join` — inner, derived-table and `\join lateral`, `\union`, `\intersect`,
  `\except`, `\select`, `\where`, `\group`, `\order`, `\case`, `\limit`,
  `\with`) parses to a pure AST, and the statements a query cannot carry —
  `\temp`, `\drop`, `\begin`, `\savepoint`, `\release`, `\rollback`, `\commit` —
  parse beside it. No hand-written SQL anywhere in the stack.
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

### Settings

Open Settings with `ctrl+comma` or the `\settings` palette action. A persistent
left sidebar lists General, Show/Hide Menus, Data source, Dialect, AI provider,
and Keybindings. Highlighting a category displays its options and explanation
in the center without changing a setting.

Use Up/Down to browse, Enter or Right to focus the category's controls, and
Enter to apply the selected action. Tab/Shift+Tab switch panels; Left or Escape
returns to the sidebar without losing the selected category. Escape from the
sidebar closes Settings. Connection and provider forms return to the same page.

## AI assistance

Open Settings with `ctrl+comma`, select **AI provider** → **Configure provider**,
and set an OpenAI-compatible **Chat Completions** base URL (including `/v1`), model, and
API key. A full `/chat/completions` URL also works. This calls your provider;
anyQL does not host an endpoint or use OMP as a dependency. The separate OpenAI
Responses API is not supported by this adapter.

For the existing yolo setup, **Import yolo key** fills `https://yolo-auto.com/v1`,
model `yolo`, and `YOLO_AUTO_API_KEY` from the environment or
`~/.omp/agent/.env`. Import only fills the form; **Save** applies it. It never
modifies `.omp`. Manual configuration supports other compatible providers.
Use HTTPS for remote providers: HTTP sends the key and conversation unencrypted.

- Type `\AI` in the document and accept the palette action with Enter. The
  assistant opens alongside the editor; the command is not query text.
- Enter in the chat input, **Send**, or `ctrl+enter` sends your message.
  Responses stream into the conversation; **Cancel** aborts a pending request,
  and Escape/**Close** hides the assistant and returns focus to the editor.
  Tab/Shift+Tab navigate chat controls while focus is inside the assistant.
- An edit response appears as a read-only **full replacement** proposal.
  **Apply** updates the editor only: it does not run a query or save a function.
  Invalid, incomplete, cancelled, or stale proposals cannot be applied. Changing
  the document or function draft invalidates its pending proposal; switching
  sources or targets clears the conversation.
- In `\fn`, click **Make with AI** at the top of the form. Nothing is sent until
  you describe what you want and press Enter or **Generate**. No name, parameters,
  or body need to be filled in first. The helper takes over the editor area;
  the source picker stays available above it.
- Review the proposed name, description, parameters, example call, and body.
  Ask for changes in the same input, or **Apply** the complete draft to the form.
  Apply returns you to the editor without saving or executing anything; use
  **Save** or **Run preview** when ready. The helper refuses proposals that would
  overwrite a different saved function. **Back**/Escape leaves the draft alone;
  **Start over** clears the conversation. Controls stay visible on small terminals,
  with a scrollable draft review.
- **Settings** in the function helper (or **AI settings** in workspace chat)
  opens provider configuration directly and returns to your unsent request.
- **New chat** clears the in-memory conversation. AI settings, keys, and chats are
  never persisted and disappear when the app exits. The key input is masked;
  provider errors do not echo response bodies or credentials.

Sending a message shares the current document/function draft with the provider.
The assistant can request read-only schema and backend capabilities, up to five
sample rows from a registered table (20 columns; long string cells truncated),
the active target source's latest ten matching successful queries, and saved
function definitions. These context tools also expose parser-only validation.
They cannot execute arbitrary model-generated queries, modify data, read files,
or run shell commands. Treat proposed changes as suggestions to review.

The existing `httpx` dependency handles SSE streaming directly. Transient HTTP
408/429/5xx and connection failures receive bounded backoff; Settings defaults
to three attempts (two automatic retries shared across a chat turn). Partial
streams are never automatically replayed. A turn permits at most six context
tool rounds; responses and tool payloads have size limits. The request timeout
also bounds each streaming request and context-tool call. Authentication errors,
truncation, and malformed streams remain visible errors, not successful edits.

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
  `\union`, `\intersect`, `\except`, `\select`, `\distinct`/`\unique`, `\where`, `\group`, `\order`,
  `\case`, `\limit`, `\with`, `\temp`, `\drop`, `\begin`, `\savepoint`,
  `\release`, `\rollback`, `\commit` — plus the
  app's actions: **Run**, **Compile**, **Data source…**, **History**. Typing a
  prefix filters the list; Enter completes the command in place and leaves a
  space for its argument.
- After a command it offers that command's arguments: table names for
  `\from`/`\open`/`\join` and the set operations (this document's `\with` names
  first, then the active source's datasets — and `all`/`distinct` ahead of the
  name until one is typed), the temp tables there are for `\drop`, column names
  (with their dtype and dataset) for
  `\select`/`\group`/`\order`, and for `\where` a three-step chain — column,
  then operator, then that column's distinct values. Numeric values are inserted
  bare, text values quoted (`\where event_type = "purchase"`); after `~`, `!~`,
  `in` or `not in` the operand is typed rather than chosen.
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

`\case`, `\limit`, `\with`, `\temp` and the transaction commands take
arguments you type yourself — the palette has no suggestions for them.

## Distinct rows

Add `\distinct` to remove duplicate output rows; `\unique` is an alias:

```anyql
\from events
\select event_type
\distinct
\order event_type
```

This returns `click`, `purchase`, `signup`, and `view` once each. Deduplication
compares the entire projected row, including computed columns, after any set
operations and before `\order`/`\limit`. Both commands take no arguments and
work in function bodies, CTEs, and inline subqueries.

## String expressions

Use Ibis string operations in `\select`, including nested calls and quoted
literal arguments:

```anyql
\from events
\select concat(upper(event_type), ':', string(user_id)) as label
\select substr(path, 1) as route
\limit 3
```

`concat` joins strings; `concat_ws(separator, ...)` joins them with a separator.
Use `string(value)` to convert numbers or other values explicitly. The catalog
also includes case conversion, whitespace trimming, length, substring, literal
replacement, contains/prefix/suffix checks, repetition, reversal, padding, and
finding a substring. `substr` and `find` use **0-based Ibis positions**.

Completion shows signatures and offers compatible argument columns and nested
functions, filtered by the active source's capabilities. NULL and Unicode
behavior follow Ibis and the executing backend. See the
[full signatures and backend limits](docs/AST.md#scalar-string-functions).
These expressions are supported in `\select`; use a CTE when filtering their
computed outputs with `\where`.

## Table-valued functions (`\fn`)

Open `\fn` from the palette to create or edit a named query with `@parameter`
arguments. The **Source target** picker starts on the workspace's current
datasource. It controls the body's IntelliSense, save-time validation, and
preview execution without changing the workspace's source or dialect. Changing
the target keeps your draft and clears the previous preview.

To reuse the current workspace query, click **To function** above the document,
or select **Query to function** from the palette. A new draft opens with the
document copied into its body and the current source selected. Give it a name,
optionally replace constants with declared `@parameters`, then choose **Save**.
Nothing runs or persists merely by opening the draft, and the workspace document
is unchanged. A function with no parameters is called with `\from name()`.

The body uses the document editor's command, table, column, function, operator,
and value suggestions, respecting the Settings IntelliSense switch. Enter or
Tab accepts a suggestion; clicking a row works too. Escape toggles IntelliSense
in the body and never leaves the library. **Ctrl+C closes the FN screen** from
any field. Tab without a popup moves to the next form field. Workspace actions
are not offered inside a body.

Type `@` in the body to offer the current **Parameters** names, or `@min` to
filter them. Suggestions follow unsaved declaration edits and stay local to
the function being edited. Enter, Tab, or a click inserts the unquoted `@name`,
including inside nested calls; quoted text and email-like tokens are ignored.
A fresh `@` reopens a dismissed popup, unless IntelliSense is disabled.

**Save** writes the function to local disk. **Run preview** (`ctrl+r`) saves
and calls the function with the preview arguments; its rows do not enter History.
Saved names, ordered parameters, descriptions, and bodies load automatically
when anyQL starts. **Delete** (`ctrl+d`) removes the definition from disk too.
Draft edits and AI **Apply** are not auto-saved.

Functions are shared across sources: the selected target is an authoring/preview
context, not a permanent binding. A later `\from name(args)` runs against that
document's active source. Reconnect your D1 source before calling its functions.

The library's **How parameters work** panel gives a complete example. On the
`demo` target, set **Name** to `events_above`, **Parameters** to `min_amount`
(no `@` in that field), and **Body** to:

```anyql
\from events
\where amount > @min_amount
\select user_id, amount
```

Set **Preview arguments** to `10` and run the preview. From the document, call
it with `\from events_above(10)` followed by `\select *`.

Parameters bind by position: `min_amount, event_kind` takes arguments such as
`10, "purchase"`, in that order. Supply one value per parameter, not
`name=value`; quote text arguments, but leave `@name` placeholders unquoted in
the body. Parameter declarations contain names only, without types or defaults.
For a function with no parameters, leave Parameters and Preview arguments blank
and call `name()`.

You can also project constants or return an argument directly in the body:
`\select @tag as tag, 'x' as marker, '1' as text_one, 1 as number_one`.
Single- and double-quoted values stay strings, even when they look numeric;
unquoted numbers, `true`, `false`, and `null` retain their scalar types.

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

Press `ctrl+o`, or open Settings (`ctrl+comma`) → **Data source** →
**Add Cloudflare D1** at the top of the center panel.

1. In the [Cloudflare dashboard](https://dash.cloudflare.com/), select your
   account and copy its **Account ID**.
2. Open **Workers & Pages → D1 → your database** and copy its database UUID.
   An exact database name also works; a Worker binding name does not.
3. Under **My Profile → API Tokens**, create a custom token with
   **Account → D1 → Read**, scoped to your intended account. Use an API token,
   not a Global API key. Cloudflare's [D1 query API](https://developers.cloudflare.com/api/resources/d1/subresources/database/methods/query/)
   accepts this permission for read queries.
4. Enter the **Account ID**, **Database UUID or name**, and masked **API token**.
   Optionally give the connection a display name. Leave **Local snapshot** blank
   to connect to hosted D1.
5. **Test connection** discovers the real tables and row counts without
   registering or saving anything. **Add** activates the live source and saves
   its account ID, resolved database UUID, and display name. Connection work runs
   in the background; editing fields or cancelling discards the pending result.
   Row counts are fetched in batches of at most five tables/views to respect
   Cloudflare's compound-SELECT limit; larger schemas are discovered in full.

On a later launch, choose the entry under **Saved connection**, paste a fresh
token, and press **Add**. Startup does not contact Cloudflare or silently fall
back to a local database. Empty hosted databases can connect with zero tables;
this form connects to an existing database rather than provisioning one.

For offline work, enter an existing SQLite database file in **Local snapshot**.
This overrides the live fields and never syncs with Cloudflare. Snapshot paths
are not remembered. No bundled snapshot is opened implicitly.
The bundled `anyql/engine/d1/d1.sqlite` is entirely synthetic: three demo
stations and twelve temperature readings, with no production data or credentials.
Regenerate it with `uv run python -m anyql.engine.make_data`; the generator
builds a fresh file rather than reusing pages from an existing database.

Live sources ship their compiled SQLite SQL to Cloudflare's D1 REST API and run
on the real engine there; snapshots run on a real SQLite engine in-process. The
**API token** you paste is used per request and kept only in memory: it is never
written to disk, never logged, and never echoed back into the UI.

## Local memory

Custom functions and token-free D1 connection profiles are stored in one
versioned `memory.json` file:

| Platform | Default location |
| --- | --- |
| Windows | `%LOCALAPPDATA%\anyql\memory.json` |
| macOS | `~/Library/Application Support/anyql/memory.json` |
| Linux | `$XDG_DATA_HOME/anyql/memory.json`, or `~/.local/share/anyql/memory.json` |

Set `ANYQL_DATA_DIR` before launching to choose a different directory. The
function editor displays the actual save path. Back up that file to keep your
library; it contains function text in plain JSON, so do not put secrets in bodies
or descriptions. Cloudflare tokens, AI keys/configuration, chats, query history,
unsaved documents, active-source choices, and temporary tables are not saved.

Writes use a temporary file and atomic replacement. Failed writes leave the
last saved library and current definitions unchanged. If another app instance
has saved newer data, copy your unsaved draft and restart rather than overwrite
it. An invalid/unreadable file produces a startup error and blocks saves: repair
it or move it aside, then restart. The original file is not silently reset.

## The results explorer

Run executes the submitted document in a background worker. Editing, scrolling,
pane visibility, and Settings navigation remain responsive. One operation owns
the shared database connection at a time: repeat Run, Compile, source/dialect
changes, source addition, and function editing are refused until it finishes. Completion
suggestions pause during execution to avoid schema/value-query races. Results
and History retain the submitted document; edits made while it runs stay in the
editor. AI context reads use the same connection reservation.

- The results table is a spreadsheet-style explorer: a **cell cursor** moves with
  the arrow keys (and page/home/end), and the executed dtypes ride along in the
  column headers (`user_id · int64`).
- `shift+left` / `shift+right` **resize the column** under the cursor.
- Focus the results grid and use **Shift+Up/Down** to select a row range,
  **Space** to toggle individual rows, **Ctrl+A** to select all buffered rows,
  and **Escape** to clear the selection. Selected rows are highlighted across
  their full width; the grid border shows selected and buffered row counts.
- **Ctrl+C** or **Copy rows** copies selected rows as tab-separated text with
  column headers. With no selection it copies the current row. Tabs, quotes,
  and embedded newlines are escaped for spreadsheet paste; NULL becomes an empty
  cell. Clipboard delivery uses the terminal's clipboard support.
- **Export CSV…** (or **Export results** in the palette) opens a destination form.
  Choose selected rows or all buffered rows and an explicit file path. Existing
  files require a separate **Overwrite file** confirmation. The UTF-8 CSV uses
  raw column names, standard CSV quoting, and empty cells for NULL.
  Export captures the current buffer; it never reruns the query and does not
  include rows beyond the preview cap. The form shows the export row count,
  buffered/total counts, and cap before writing.
- `NULL` values render as `NULL` rather than as blank cells.
- The table buffers the **first 10 000 rows** of a result. When a document
  returns more than that, the status line says so — `showing 10000 of 12000
  (preview capped)` — while the run itself still executed the document exactly
  as written.
- A refused run never crashes the app: the parser's message (with its line
  number) appears on the Results tab, and the footer repeats it as the status.
- A `\temp` run says what it kept (`created temp table "event_totals" · 4 rows · …`)
  and the explorer lists the table, marked `temp`; a `\drop` takes it away again.

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
written as wrapped strings. Beyond the clauses above:

- **Row counts**: `count(*)` counts every joined/filtered row, including NULLs;
  `count(column)` counts only non-NULL values. Both support groups and window
  frames. Use an explicit alias such as `count(*) as total_requests`.
- **Subqueries** are inline, on the clause's own line and in the same grammar:
  `\from (\from events \select user_id, amount \where amount > 5) as big`,
  `\join ( … ) as u on e.user_id = u.user_id`, a set-op operand
  (`\union all ( … )`), `\where user_id in (\from users \select user_id \where
  region = "us")`, and a scalar `\select (\from events \select max(amount)) as
  peak`.
- **Lateral joins / CROSS APPLY**: `\join lateral ( … ) as recent` lets the body
  read the tables to its left, and a `\limit` inside it means *per left row* —
  `\join lateral (\from events e \where e.user_id = u.user_id \order timestamp
  desc \limit 2) as recent` is two events for each user. Leave the `on` off for
  the `CROSS APPLY` spelling.
- **Window frames**: `over (partition by user_id order by timestamp rows between
  unbounded preceding and current row)` — `rows` counts rows, `range` counts
  ordering values, and either end may be unbounded.
- **Regular expressions**: `\where path ~ "/p/[12]"` (and `!~` to negate),
  `regexp_extract(path, "/p/([0-9]+)", 1)` and
  `regexp_replace(path, "/p/[0-9]+", "/page")`.
- **Temp tables**: `\temp event_totals` keeps the document's rows as a table the
  rest of the session can name (`\from event_totals`); `\drop event_totals`
  removes it. They live on the connection and die with the process.
  Creation refuses `sqlite_` and `_cf_` prefixes, case-insensitively. Creation,
  replacement, and drop target only the temporary namespace, never persistent
  snapshot tables, including tables hidden from the schema explorer.
- **Transactions**: `\begin` opens one, `\commit`/`\rollback` end it,
  `\savepoint s` / `\rollback to s` / `\release s` sit inside it. Each run is one
  statement, so `\begin` in one run and `\rollback` in the next is the usual
  shape; what it buys is a temp table that a rollback takes back out. DuckDB has
  no savepoints, so those are offered (and named) only on a SQLite snapshot.

The full contract — every command and argument rule, star projections, `over (
… )` frames, `\case`, table aliases, the exact error strings — lives in
**[docs/AST.md](docs/AST.md)**.

## Tests

```bash
uv run pytest -q                          # 279 tests: the language/engine contract + the TUI end to end
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
                transactions/savepoints/temp tables (tx.py), deterministic demo
                data, make_data.py
  tui/          the app: widgets, session core, `\` palette, add-source modal, styles
tests/          pytest suite + checked-in fixtures
spec/           canonical document ↔ AST pair (the language contract)
docs/AST.md     the language contract
verify/         check_canonical.py — the language gate
```
