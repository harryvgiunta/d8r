# D8R

Pronounced “Datuhr” — a playful take on “data”.

**A keyboard-first IDE for querying data — one document, any datasource, any
dialect, right in your terminal.**

D8R is a real application, not a mockup. It is a pure-Python
[Textual](https://textual.textualize.io) TUI: what you type is a document in a
short `\command` language, that document is parsed into an AST, the AST builds a
real [ibis](https://ibis-project.org) expression, and the rows you see come from
real query execution on an in-process DuckDB — or, if you add one, on your own
Cloudflare D1 or PostgreSQL database. D8R itself starts no server and needs no
browser. `uv run d8r` is the whole app; a PostgreSQL source needs a reachable database.

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
git clone https://github.com/harryvgiunta/d8r.git
cd d8r
uv sync          # create/refresh the project environment (.venv)
uv run d8r     # launch the IDE
```

Dependencies are declared in `pyproject.toml` and locked in `uv.lock`; nothing
is installed globally, and every command runs in the project's `.venv`. Python
`>=3.13` is required (pinned in `.python-version`). `uv run python -m d8r` is
the equivalent module form.

A fresh session opens on the `demo` source with a real query already in the
editor, so `ctrl+enter` does something true on the very first keystroke.

## The screen

```
┌ D8R   [ Local demo · DuckDB + Parquet ▾ ]  [ DuckDB ▾ ]  duckdb ───────────┐
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
  `sqlite (Cloudflare D1)`, `postgres (live)`). Switching either picker re-points the whole app:
  the tree, the parser's schema seam, and the palette's offers.
- **Schema pane** — the active source's datasets with their row counts (PostgreSQL
  counts are not loaded, avoiding full-table scans during connection). Expand a
  dataset to load its columns (name + dtype) and select one to insert its name
  into the document.
- **Document pane** — the editor (line numbers on), with the `\` palette
  floating under the caret.
- **Results / SQL / History tabs** — Results shows the executed rows, or the
  parser/engine error line when a run is refused; SQL shows the compiled
  document for the active dialect (read-only, never executed); History lists
  saved successful runs across sessions; selecting a row restores its document,
  source target and dialect without executing it or reconnecting automatically.
- **Footer** — the key hints, and a status line that reports what just happened
  (rows, milliseconds, source, dialect).

### Settings

Open Settings with `ctrl+comma` or the `\settings` palette action. A persistent
left sidebar lists General, Show/Hide Menus, Data source, Dialect, AI provider,
and Keybindings. Highlighting a category displays its options and explanation
in the center without changing a setting.

Use Up/Down to browse, Enter or Right to focus the category's controls, and
Enter or click a row to toggle, select, or edit it; the footer names the action
for the current category. Tab/Shift+Tab switch panels; Left or Escape returns
to the sidebar without losing the selected category. Escape from the sidebar
closes Settings. Connection and provider forms return to the same page and row.

Preferences are loaded from `~/.d8r/settings.json` and saved when changed.
You can also edit the JSON while D8R is closed; restart to load edits. Settings
shows the actual file path. See [Configuration and backups](#configuration-and-backups)
for the directory override, file format, and credential handling.

**General → Default rows returned** starts at **50**. Save a whole number from
0 to 1,000,000; **0 disables the default**. Ordinary Run and Compile apply it only
when the top-level query has no explicit `\limit`. An explicit `\limit`, including
`\limit 0`, wins. The document and AST remain unchanged. Temporary-table creation
uses exactly the query's own limits, and AI sample reads keep their separate cap.
The results pane's independent 10,000-row preview buffer still applies.

### Workspace recovery

The editor document/caret, selected source/dialect, last workspace or function
view, unsaved function draft/target, query history, and target-scoped AI chats
are autosaved to `workspace.json`. Editing is debounced and the latest widget
contents are flushed on exit, including an unsent AI message. Function drafts
remain drafts: only **Save**/**Run preview** changes the function library.

Startup restores this state without running queries, sending AI requests, or
opening database connections. A saved D1 database or SQLite snapshot remains
visibly **disconnected** until explicitly reconnected; its draft never silently
runs against demo data. Local snapshot paths are remembered for reconnection.
Use the header's **Reconnect** button or select the saved source in Settings to
reconnect deliberately. `ctrl+o` still opens the add-source form without making
a live connection just by opening it; a restored snapshot path is prefilled.
Result grids, temporary tables, transaction state, diagnostic logs, in-flight
requests, and applicable AI proposals are not restored. A restored conversation
can continue on Send, using freshly captured context and validation.

## AI assistance

Open Settings with `ctrl+comma`, select **AI provider**, then press Enter or click
**Endpoint**, **Model**, **API key**, **AI turns/tool rounds**, **Tool calls per round**,
**Sample records per read**, **Maximum attempts**, or **Request timeout**.
The provider form opens with that field focused. Edit an OpenAI-compatible
**Chat Completions** base URL (including `/v1`), model, and masked API key, then
choose **Save** to persist changes; **Cancel** or Escape discards them.
A full `/chat/completions` URL also works. This calls your provider;
D8R does not host an endpoint or use OMP as a dependency. The separate OpenAI
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
  sources or targets switches to that target's own saved conversation.
- In `\fn`, click **Make with AI** at the top of the form. Nothing is sent until
  you describe what you want and press Enter or **Generate**. No name, parameters,
  or body need to be filled in first. The helper takes over the editor area;
  the source picker stays available above it.
- Review the proposed name, description, parameters, example call, and body.
  Ask for changes in the same input, or **Apply** the complete draft to the form.
  Apply returns you to the editor without defining a function or executing it;
  the draft is autosaved. Use **Save** or **Run preview** when ready. The helper refuses proposals that would
  overwrite a different saved function. **Back**/Escape leaves the draft alone;
  **Start over** clears the conversation. Controls stay visible on small terminals,
  with a scrollable draft review.
- **Settings** in the function helper (or **AI settings** in workspace chat)
  opens provider configuration directly and returns to your unsent request.
- **New chat** durably clears the current target's conversation, not other
  targets. Completed chat exchanges and the composer draft survive exit;
  incomplete replies never enter replayable history. **Save** in AI settings
  persists the provider URL, model, key, tool-round/call/sample limits, attempts,
  and timeout. The key input stays masked; provider errors do not echo response
  bodies or credentials. Protect the local JSON files and their backups.

Sending a message shares the current document/function draft with the provider.
The assistant can request read-only schema and backend capabilities, bounded
sample rows from a registered table (5 by default; at most 20 columns, with long
string cells truncated),
the active target source's latest ten matching successful queries, and saved
function definitions. These context tools also expose parser-only validation.
They cannot execute arbitrary model-generated queries, modify data, read files,
or run shell commands. Treat proposed changes as suggestions to review.

The existing `httpx` dependency handles SSE streaming directly. Transient HTTP
408/429/5xx and connection failures receive bounded backoff; Settings defaults
to three attempts (two automatic retries shared across a chat turn). Partial
streams are never automatically replayed. Each user message permits up to the
configured number of context tool rounds (10 by default), with the remaining
budget sent to the model. After that budget is used,
tools are disabled for a final response using the collected context; if it is
insufficient, the assistant is instructed to explain what is missing instead of
inventing a query. A provider that ignores disabled tools still fails safely.
Responses and tool payloads have size limits. The request timeout also bounds
each streaming request and context-tool call. Authentication errors, truncation,
and malformed streams remain visible errors, not successful edits.
Editor instructions and request-local tool-budget guidance share one initial
system message, including on retries and follow-up requests, for providers with
strict chat templates such as Yolo. The guidance never accumulates in chat history.

Provider limits are independent and saved together only on **Save**:

| Setting | Default | Allowed | Meaning |
| --- | --- | --- | --- |
| AI turns/tool rounds | 10 | 1–50 | Context batches per user message, followed by one final tools-disabled answer |
| Tool calls per round | 16 | 1–16 | Maximum calls in one model response; an oversized batch is rejected before any call executes |
| Sample records per read | 5 | 1–100 | Maximum rows in each sample; an omitted tool limit uses this value |
| Maximum attempts | 3 | 1–5 | One initial attempt plus a shared allowance of `attempts - 1` transient retries across the message |
| Request timeout | 60 seconds | >0–300 seconds | Time bound for each provider request or context-tool call |

A tool round is not another chat message: it may batch schema reads, sample
reads, history, function lookups, and validation calls. Those calls share the
round and per-round call budgets; sampled records do not count as extra turns.
Retries do not consume additional tool rounds or reset their budget. Increasing
rounds or samples can increase latency, provider usage, and data sent. The
existing 20-column, 500-character string-cell, and 256-KiB tool-result limits
still apply; oversized results fail safely rather than bypassing the byte cap.
Limits are captured when sending a message; settings changes affect later
messages, not in-flight work. Older settings files receive missing defaults in
memory without being rewritten on load. **Import yolo key** preserves budget
values already entered in the form.

The assistant's language guide covers composing multiple filters with CTEs or
inline subqueries, joining filtered inputs, and grouping daily counts without
inventing SQL syntax. Each request includes the current UTC time. Relative-date
requests use explicit literal bounds with the range/timezone stated in the reply:
these are fixed when generated, not a rolling expression when rerun. Parser
validation does not verify execution or column-type compatibility.

For exclusions such as “free users who never subscribed,” the guide recommends
projecting matching keys on both sides of `\except`, then joining surviving keys
back to the detail table. It distinguishes lifetime purchase history from current
subscription status and calls out nullable-key pitfalls with `not in`.
Set operations accept nullability-only type differences (`!string` versus
`string`) without changing values; genuinely different value types still fail.
Whether a ledger includes trials, failed payments, or manual grants remains a
business-rule question, not something syntax validation can establish.

### AI failure diagnostics

Choose **Logs** in workspace chat or the function helper to inspect the current
chat's diagnostic trace. It opens at the latest entries; scroll up for earlier
attempts. **Refresh** updates the snapshot while a request runs, **Copy logs**
copies the displayed trace, and Escape/**Close** returns to the assistant.

The trace includes UTC timestamps, model/request settings (not credentials or
endpoint URLs), the submitted prompt and editor context, tool-round counts,
HTTP status/retries, model output, requested and executed tool calls with their
arguments/results, parser validation, failures, cancellation, and explicit Apply.
Incomplete output is labeled and never becomes an applicable proposal. Internal
failures include the exception type and code locations, not exception text or
locals. Raw provider error bodies, HTTP headers, and hidden model reasoning are
not logged. Known AI-provider keys and saved/connected D1 tokens are redacted.

Failed attempts remain inspectable after another message, but are **not** added
to the AI conversation. Diagnostics are never sent back to the model. Retention
is bounded to 256 entries / 262,144 characters, with at most 65,536 characters
per entry; truncation and eviction are marked. Logs are in memory only: New chat,
Start over, a target/source switch, or exiting discards them. Copy anything needed
before doing so. Logs can contain schema, sample values, and query text; review
them before sharing. There is no automatic log file.

For capability investigations, inspect the attempted `validate_d8r` text and
its error alongside the returned schema/capabilities. That validator checks the
D8R parser, not Ibis execution: rejection may identify a missing D8R mapping
for an operation that Ibis supports. A parser-valid draft can still fail later
during compilation or execution. Such Run errors remain in the Results pane;
the AI diagnostic log does not execute proposals to diagnose them.

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

```d8r
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

```d8r
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
when D8R starts. **Delete** (`ctrl+d`) removes the definition from disk too.
Draft edits and AI **Apply** are autosaved separately in `workspace.json`, not
committed as function definitions. Returning from the function editor refreshes
IntelliSense at the existing caret, so newly saved calls appear without retyping;
disabled or Escape-dismissed completion stays closed.

Functions are shared across sources: the selected target is an authoring/preview
context, not a permanent binding. A later `\from name(args)` runs against that
document's active source. Reconnect your D1 source before calling its functions.

The library's **How parameters work** panel gives a complete example. On the
`demo` target, set **Name** to `events_above`, **Parameters** to `min_amount`
(no `@` in that field), and **Body** to:

```d8r
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

## Datasources, dialects, and function compatibility

The per-dialect and per-backend function matrix — which catalog calls each
compile target can render and each live source can run — is documented in
[docs/COMPATIBILITY.md](docs/COMPATIBILITY.md) (with the sectors and the
function catalog). It is regenerated from `probe_functions.py`.

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

## Add a PostgreSQL database

Press `ctrl+o` (or Settings → **Data source** → **Add data source**), then choose
**PostgreSQL** in **Backend**. Enter host, port, database, user, masked password,
schema (default `public`), and TLS/SSL mode. This is a **real Ibis PostgreSQL
connection**, not the bundled PostgreSQL mock. Tables and views in the selected
schema populate the explorer; connect separately to use another schema. Empty
schemas work, and connection discovery does not count all rows or install extensions.

Use **Test connection** before **Add**. Only Add saves the profile and password
in local `memory.json`; Test and Cancel save nothing. Password whitespace is
preserved. Saved profiles appear as disconnected after restart and reconnect only
when explicitly selected. Changing the SQL dialect affects rendering, not the
database on which Run executes.

For production, use a least-privileged read-only role and the TLS mode required by
your provider. `verify-full` validates certificates and hostnames using libpq's
certificate configuration; the default `prefer` allows an unencrypted connection.
Queries use Ibis expressions. Transactions, savepoints and session-local temp
tables use the same connection; temporary DDL cannot target persistent tables.

### Local PostgreSQL with Docker

`compose.yaml` runs PostgreSQL 17.9 on **127.0.0.1:55433**, with database/user
**d8r**, a health check, and a named volume. It does not replace other containers
or expose the database on your LAN. Put a strong development password in the
gitignored `.env.postgres` as `D8R_POSTGRES_PASSWORD=...`; never commit that file.

```sh
docker compose --env-file .env.postgres up -d --wait postgres
docker compose --env-file .env.postgres stop postgres
```

Stop preserves the volume. The password initializes a new volume only; changing
the environment file does not rotate an existing database password. For the
connection form, use host `127.0.0.1`, port `55433`, database/user `d8r`, schema
`public`, and SSL mode `prefer` for this local development container.

Ibis's own [Compose services](https://github.com/ibis-project/ibis/blob/main/compose.yaml)
use per-backend images and readiness checks. Its
[PostgreSQL image](https://github.com/ibis-project/ibis/blob/main/docker/postgres/Dockerfile)
also installs spatial/vector extensions for its broader test matrix; this app's
container uses stock PostgreSQL. Its
[pytest backend fixture](https://github.com/ibis-project/ibis/blob/main/ibis/backends/postgres/tests/conftest.py)
connects through Ibis with environment-supplied connection parameters.

The normal D8R test suite is offline. To exercise PostgreSQL integration tests,
set `D8R_TEST_POSTGRES=1` and `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`,
`PGPASSWORD` before `uv run pytest -q`. Against the local container above that
is `PGHOST=127.0.0.1 PGPORT=55433 PGDATABASE=d8r PGUSER=d8r`, with `PGPASSWORD`
from `.env.postgres`. Use a **disposable test database**: these tests create and
remove uniquely named schemas. `tests/test_backend_matrix.py` then runs every
parity document on DuckDB/Parquet, the SQLite snapshot, **and** live
PostgreSQL, asserting identical rows; without the gate it runs the two local
backends only.

## Add a Cloudflare D1 database

Press `ctrl+o`, or open Settings (`ctrl+comma`) → **Data source** →
**Add data source**, then choose **Cloudflare D1 / local SQLite snapshot**.

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
   its account ID, resolved database UUID, display name, and API token. Connection work runs
   in the background; editing fields or cancelling discards the pending result.
   Row counts are fetched in batches of at most five tables/views to respect
   Cloudflare's compound-SELECT limit; larger schemas are discovered in full.

On later launches, saved D1 profiles remain in the main **datasource dropdown**,
Settings → **Data source**, and the function editor's source picker, marked
**disconnected**. Selecting one opens the connection dialog and immediately
connects using its saved credentials. Startup and merely opening the dropdown
do not contact Cloudflare. The active source stays unchanged until connection
succeeds; failure or Cancel leaves the saved choice available for another attempt.
Once connected, it appears as a normal source, without a duplicate saved entry.
Reconnecting an unchanged profile does not rewrite its credentials. Connecting
from the function picker changes only the function target, not the workspace.

Older profiles without a token open the masked token field without attempting a
connection. Enter the token and choose **Connect**. You can also use `ctrl+o` →
**Saved connection** → **Add** to inspect or update credentials. Empty hosted
databases can connect with zero tables; this form connects rather than provisions.

For offline work, enter an existing SQLite database file in **Local snapshot**.
This overrides the live fields and never syncs with Cloudflare. Snapshot paths
are not remembered. No bundled snapshot is opened implicitly.
The bundled `d8r/engine/d1/d1.sqlite` is entirely synthetic: three demo
stations and twelve temperature readings, with no production data or credentials.
Regenerate it with `uv run python -m d8r.engine.make_data`; the generator
builds a fresh file rather than reusing pages from an existing database.

Live sources ship their compiled SQLite SQL to Cloudflare's D1 REST API and run
on the real engine there; snapshots run on a real SQLite engine in-process. The
**API token** is used per request and saved locally on **Add**, or on **Connect**
when you change the saved profile's fields. It is never
logged or displayed unmasked. **Test connection** and **Cancel** save nothing.

## Configuration and backups

The default home is `~/.d8r` on every platform (`%USERPROFILE%\.d8r` on
Windows). Set `D8R_DATA_DIR` before launching to use another directory:

```powershell
$env:D8R_DATA_DIR = "D:\d8r-home"
uv run d8r
```

On macOS/Linux: `D8R_DATA_DIR=/path/to/d8r-home uv run d8r`.
An explicit `Session(data_dir=...)` takes precedence for embedded/headless use.

| File | Saved contents |
| --- | --- |
| `settings.json` | Intellisense, pane visibility, selected source/dialect, default returned rows, AI provider URL/model/API key, tool-round/call/sample limits, attempts, timeout |
| `memory.json` | Custom functions; D1 profiles and API tokens; PostgreSQL host/port/database/user/schema/TLS profiles and passwords |
| `workspace.json` | Document/caret and stable target identity, last view, function draft, query history, completed AI conversations/composer drafts, source references and snapshot paths |

These files are versioned, editable JSON. Toggle preferences save immediately;
the provider and default-row forms require **Save**. Import only fills the
provider form, and Cancel discards it. D1/PostgreSQL credentials save on **Add** or after
edited fields are submitted with **Connect**; function definitions persist on
**Save**/**Run preview** or Delete. Workspace drafts and histories autosave
independently. Loading never connects, executes, or saves a function definition.
Unavailable workspace targets restore as disconnected, preserving their identity
and drafts until explicitly reconnected or replaced with another source.

A `settings.json` example (omitted keys use defaults):

```json
{
  "version": 1,
  "intellisense": true,
  "panes": {"results": true, "sql": true, "history": false, "schema": true},
  "source": "demo",
  "dialect": "duckdb",
  "default_rows": 50,
  "ai": {
    "base_url": "https://provider.example/v1",
    "model": "your-model",
    "api_key": "",
    "max_attempts": 3,
    "timeout": 60,
    "max_tool_rounds": 10,
    "max_tool_calls": 16,
    "sample_rows": 5
  }
}
```

Leave `ai` out for an unconfigured provider. Unknown keys or invalid values
produce a startup error rather than being silently discarded. Changes made
outside the app take effect on restart, not through live reload.

**Back up all three JSON files to retain settings, functions, credentials, and
your workspace. They are plaintext, not encrypted.** In addition to credentials
in settings/memory, workspace chats can contain schema, sampled data, query text,
and unsent drafts. Keep backups private and never commit them to source control.
Restore files into the chosen home while D8R is closed. New directories/files
are owner-only on POSIX; Windows uses filesystem ACLs. Result buffers, temporary
tables, transactions, partial AI streams and diagnostic logs remain memory-only.

For an existing installation, copy `memory.json` from the former platform data
directory into `~/.d8r`, or point `D8R_DATA_DIR` at that existing directory.
Former defaults were `%LOCALAPPDATA%\d8r` on Windows,
`~/Library/Application Support/d8r` on macOS, and `$XDG_DATA_HOME/d8r` (or
`~/.local/share/d8r`) on Linux. No files are moved automatically. Old profiles
without tokens load unchanged; their next **Add**/**Connect** saves the supplied token.

Writes use a temporary file, a save lock, and atomic replacement. Failed writes
leave saved data and current settings unchanged; unsaved workspace drafts and
completed query results remain available in memory, with a visible save error.
Editor, chat and query-history writes merge under one session lock. If another
app instance or editor changes a file, copy your unsaved work and restart rather
than overwrite it. A lock or temp file left by a crashed save clears itself after a minute.
An invalid/unreadable file is preserved and blocks saves to that file: repair it
or move it aside, then restart. It is never silently reset.

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

```d8r
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
d8r/
  __main__.py   `uv run python -m d8r`
  query/        pure language layer (parser, AST, alias rules, immutable schema context)
  engine/       ibis/DuckDB execution, datasource registry, dialects, D1 client,
                transactions/savepoints/temp tables (tx.py), deterministic demo
                data, make_data.py
  tui/          the app: widgets, session core, `\` palette, add-source modal, styles
tests/          pytest suite + checked-in fixtures
spec/           canonical document ↔ AST pair (the language contract)
docs/AST.md     the language contract
verify/         check_canonical.py — the language gate
```
