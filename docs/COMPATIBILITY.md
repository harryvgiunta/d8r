# D8R — sectors, functionality, function compatibility

The application-wide map: what each sector does, what the language can
express, and — measured, not remembered — which catalog functions each
dialect and each live backend can actually compile and run.

The compatibility facts here were produced by probing the installed
ibis 12.0.0 directly (`uv run python probe_functions.py`, which compiles
one representative call per function for all 19 compilable dialects and
executes it on each live connection) and by reading each fact back off
`capabilities_for` (`d8r/engine/datasources.py`), which derives the same
answers from the app's own live probes. Where a function is advertised
per source, the probe agrees with it.

---

## 1. Sectors

Three layers, one direction of data flow
(text → AST → payload → ibis expression → rows → widgets). Each sector
consumes the one above it and none re-implements a rule of another.

### 1.1 Language — `d8r/query/` (pure, no state, no Ibis)

|Module|Functionality|
|---|---|
|`parser.py`|`parse_query(doc, schema=…, settled=…)` — the whole grammar: commands, `\where` condition **trees** (`and`/`or`, `( … )` groups, `MAX_WHERE_DEPTH = 32`), `\case`, windows/frames, set ops, inline subqueries, lateral joins, CTEs, statements. Errors land on the AST as `errors`, never exceptions. `payload_from_ast` maps the AST to the engine payload. Clause lookup helpers (`clause_line`, `block_extent`) serve the palette.|
|`ast.py`|Frozen dataclasses + `to_json()` — the documented shape mirrored by `spec/canonical-query.ast.json`.|
|`functions.py`|`SCALAR_FUNCTIONS`: the 34 catalog calls with parameter kinds, arity (`minimum`, variadic), and result kinds. `DUCKDB_UNRENDERABLE` names what this build cannot compile on DuckDB (the 5 URL accessors + `convert_base`).|
|`schema.py`|`SchemaContext` — the one immutable seam the parser and palette read: tables, columns, dtype families, functions. `AGGREGATES`/`TEMPORAL` name what the grammar admits.|
|`alias.py`|Derived output names (`sum(amount)` → `amount_sum`).|

Boundaries: the parser touches only `SchemaContext.tables`/`.fns`; the
last non-empty line is the typing line (quiet while growing); numeric
tokens ≤ 640 chars; ≤ 16 nested / 256 total function calls per parse.

### 1.2 Engine — `d8r/engine/` (payload in, SQL + rows out)

|Module|Functionality|
|---|---|
|`datasources.py`|The registry: `demo` (Parquet on DuckDB), four vendor mocks, runtime-added SQLite snapshots (`d1`), live Cloudflare D1 (`d1-live`), live PostgreSQL (`postgres-live`). `DIALECTS` (20 compile targets), `CAPABILITIES`, `capabilities_for` (per-source truth: function lists, operator lists, `supports` flags — live-probed per connection through `_probe_flags`/`_unrenderable_on`, plus a `pg_proc` check for extension-backed functions like `levenshtein`).|
|`expression.py`|`build` — payload → `ir.Table`: select items (column / literal / aggregate / temporal / scalar call / regex / rank / window / case / subquery), the recursive `_condition_tree` for `\where` (ands, or-groups, `( … )` groups), joins incl. the lateral correlation hoisting + top-N rewrite, set ops, `compile_sql` (render-only, any dialect).|
|`execute.py`|`execute` (build → compile → run), `execute_remote` (live D1: the compiled SQLite SQL POSTed to D1's HTTP API), `materialize` (`\temp`).|
|`tx.py`|Transactions, savepoints, temp tables — the only DDL in the app, issued through the backend handle (`CREATE TEMPORARY TABLE … AS`, temp-namespace-qualified).|
|`d1api.py`|Cloudflare D1 REST client (the user's token, never logged).|
|`make_data.py`|Deterministic Parquet + SQLite fixtures; byte-identical regeneration.|

### 1.3 TUI — `d8r/tui/` (consumes both layers; no network of its own except the AI client)

|Module|Functionality|
|---|---|
|`session.py`|Headless core: `run`/`compile`, history, `refresh_schema` (one immutable `SchemaContext` per session state), default-row limit, preview cap, transaction state.|
|`app.py`|Textual widgets + bindings; panes as tabs; results explorer with column width keys.|
|`palette.py`|What `\` offers: commands, datasets, columns, operators, values, joiners — including `\where` completion that reads through `( … )` groups, always filtered by `capabilities_for`.|
|`settings.py` / `add_source.py` / `fn.py` / `ai.py` / `storage.py` / `splash.py`|Settings menu; the add-source modal; the function library/editor; the AI chat panel; durable workspace/settings storage; boot animation.|

---

## 2. Language functionality (what a document can say)

One document = one statement, assembled from `\`-commands in any order:

|Group|Commands|
|---|---|
|Sources|`\from` / `\open`, `\join` (incl. `\join lateral`), `\with`, set ops `\union` / `\intersect` / `\except` (with `all` / `distinct` / a name / an inline body)|
|Projection|`\select` (columns, `*`, literals, aggregates, temporal calls, scalar calls, `rank()/dense_rank()/row_number() over (…)`, windows with `rows/range between …`, `\case … when … then … else … end`, inline subqueries as `( \from … )`), `\distinct` / `\unique`|
|Filtering|`\where` — a condition tree: `col op value`, `between`, `in (list|subquery)`, `is [not] null`, `like` / `ilike`, `~` / `!~` (regex), composed with `and` / `or` and nested `( … )` groups|
|Ordering / size|`\group`, `\order` (`asc`/`desc`), `\limit`|
|Statements|`\temp`, `\drop`, `\begin`, `\savepoint`, `\release`, `\rollback`, `\commit` (run in document order, before the query)|
|Workspace|`\fn`, `\AI`, `\settings`, `\results`, `\sql`, `\history`, `\schema`|

Operators: `=` `!=` `>` `>=` `<` `<=` `like` `ilike` `~` `!~` `in`
`not in` `between` `is null` `is not null` (subject to per-source
capability flags, §4.4).

---

## 3. Function catalog (the callable set)

Everything the language can compute. Arity and argument kinds are enforced
by the parser (`d8r/query/functions.py`); result kinds are ibis's.

**Aggregates** (`\select <fn>(<col>)`, exactly five):
`sum`, `avg`, `count`, `min`, `max` — over every dtype family the column
admits (`count` takes `*` too). No median/quantile/unnest surface in the
document language; the `supports` flags for those describe the probed
ibis layer, not the grammar.

**Temporal extraction** (dtype-gated):
`year`, `month`, `day`, `quarter` (timestamp/date) · `hour`, `minute`,
`second` (timestamp/time).

**Window ranks** (`<fn>() over ( … )`, rendered 1-based):
`rank`, `dense_rank`, `row_number`.

**Scalar calls** (34; nestable; literals allowed as arguments):

|Family|Functions|
|---|---|
|Cross-type|`string(x)` · `coalesce(a, b, …)` (variadic) · `nullif(a, b)`|
|String|`concat` (variadic) · `concat_ws` (variadic) · `lower` · `upper` · `capitalize` · `length` · `strip` · `lstrip` · `rstrip` · `substr(s, start[, len])` (0-based) · `left` · `right` · `replace` · `contains` · `startswith` · `endswith` · `repeat` · `reverse` · `lpad` · `rpad` · `find` (0-based) · `translate` · `levenshtein`|
|URL|`url_protocol` · `url_host` · `url_path` · `url_query` · `url_fragment`|
|Temporal parse|`as_date(s, fmt)` → date · `as_time(s, fmt)` → time · `as_timestamp(s, fmt)` → timestamp|
|Numeric text|`convert_base(s, from, to)`|

---

## 4. Function × language compatibility

Two independent questions, answered separately and measured live:

* **Compile** — `ctrl+k` renders the document's SQL for *any* of the 19
  compilable dialects without executing anything (the 20th, PySpark, is
  listed as known-but-uncompilable). A function "compiles" for a dialect
  when ibis has a rendering rule there.
* **Execute** — a source runs a function only when its own connection
  answers; `capabilities_for` hides what it cannot, and the palette stops
  offering it.

### 4.1 Universal (compiles 19/19 and executes on every live backend)

`sum`, `avg`, `count`, `min`, `max` · `year`, `month`, `day`, `quarter`,
`hour`, `minute`, `second` · `rank`, `dense_rank`, `row_number` ·
`string`, `coalesce`, `nullif` · `concat`, `concat_ws`, `lower`, `upper`,
`capitalize`, `length`, `strip`, `lstrip`, `rstrip`, `substr`, `left`,
`right`, `replace`, `contains`, `startswith`, `endswith`, `repeat`,
`reverse`, `lpad`, `rpad`, `find` (the last with `concat_ws` compiling
everywhere except Exasol).

### 4.2 Compile matrix — per-dialect gaps

Every language function compiles on every dialect **except** the gaps
below (measured against ibis 12.0.0; `·`-listed = cannot render):

|Dialect|Does not compile|
|---|---|
|`duckdb`|`url_*` (all 5) · `convert_base`|
|`athena`|`as_time` · `convert_base`|
|`bigquery`|`convert_base`|
|`clickhouse`|`levenshtein` · `as_date` · `as_time` · `as_timestamp` · `convert_base`|
|`databricks`|`url_*` · `as_time` · `convert_base`|
|`datafusion`|`as_date` · `as_time` · `as_timestamp` · `convert_base`|
|`druid`|`translate` · `levenshtein` · `url_*` · `as_date` · `as_time` · `as_timestamp` · `convert_base`|
|`exasol`|`concat_ws` · `levenshtein` · `url_*` · `as_date` · `as_time` · `as_timestamp` · `convert_base`|
|`flink`|`translate` · `levenshtein` · `as_time` · `convert_base`|
|`impala`|`levenshtein` · `as_time` · **`convert_base` compiles here only**|
|`materialize`|`levenshtein` · `url_*` · `convert_base`|
|`mssql`|`levenshtein` · `url_*` · `as_date` · `as_time` · `as_timestamp` · `convert_base`|
|`mysql`|`translate` · `levenshtein` · `url_*` · `convert_base`|
|`oracle`|`url_*` · `as_time` · `convert_base`|
|`postgres`|`url_*` · `convert_base`|
|`risingwave`|`url_*` · `convert_base`|
|`snowflake`|`url_host` · `convert_base`|
|`sqlite`|`levenshtein` · `as_date` · `as_time` · `as_timestamp` · `convert_base`|
|`trino`|`as_time` · `convert_base`|
|`pyspark`|*known-but-uncompilable in this build* (labelled `· unavailable` in the picker)|

Notes:

* `convert_base` is effectively **render-only on this backend set**: no
  live source of this app can run it, and only Impala can even compile
  it. It remains in the catalog because custom functions may reach it.
* The SQLite compile gaps are about the *plain* dialect; the app's local
  snapshot connection closes some of them with registered UDFs (§4.3).

### 4.3 Execution matrix — live backends

What each source actually runs, per function family
(`uv run pytest` covers these cells in `tests/test_backend_matrix.py`;
the columns are what `capabilities_for` advertises — measured by
compiling/executing against that very connection):

|Function family|demo + mocks<br>(DuckDB)|SQLite snapshot<br>(`kind="d1"`)|PostgreSQL live|Cloudflare D1<br>(live, over HTTP)|
|---|---|---|---|---|
|5 aggregates, 7 temporal, 3 ranks, cross-type, plain string family|✓|✓|✓|✓|
|`translate`|✓|✓ (UDF `_IBIS_TRANSLATE`)|✓|✗|
|`levenshtein`|✓|✗|✗ unless `fuzzystrmatch` is installed (checked via `pg_proc`, never installed)|✗|
|`as_date` / `as_timestamp`|✓|✗|✓|✗|
|`as_time`|✓|✗|✓|✗|
|`url_*` (5)|✗|✓ (registered Python UDFs)|✗|✗|
|`convert_base`|✗|✗|✗|✗|

The demo's advertised string-function list is exactly the 26 it can
compile (everything except URL + `convert_base`); the snapshot advertises
the 27 it can run (no `levenshtein`/`as_*`/`convert_base`, plus the five
URL UDFs); live PostgreSQL advertises 25 (no `levenshtein` on a stock
server, no URL, no `convert_base`); live D1 advertises 17 (plain SQLite
SQL only — everything ibis would render as a `_IBIS_*` UDF or a rule
Cloudflare's SQLite lacks is dropped, `capitalize` included stays, since
it compiles to `UPPER`/`SUBSTRING`).

### 4.4 Capability flags per source kind

`supports` as advertised by `capabilities_for` (live-probed; `False`
defaults unless the connection proves otherwise):

|Flag|demo / mocks<br>(DuckDB)|SQLite snapshot|PostgreSQL live|D1 live|
|---|---|---|---|---|
|`groupBy` `orderBy` `limit` `distinct` `case` `cte` `like` `ilike` `window` `subquery` `lateral` `frame`|✓|✓|✓|✓|
|`regex` (`~`, `!~`)|✓|✓ (UDF)|✓ on PostgreSQL ≥ 15 (`regexp_like`); hidden below|✗ (UDF-only rendering)|
|`temp`|✓|✓|✓|✗ (stateless HTTP)|
|`transactions`|✓|✓|✓|✗|
|`savepoints`|✗ (whole transactions only)|✓|✓|✗|
|`quantile`|✓|✗|✓|✗|
|`asofJoin`|✓|✗|✓|✗|
|`sampling`|✓|✓ (`WHERE random() <= f`)|✓|✓|
|`samplingSeed`|✓|✗|✓ (per connection probe)|✗|
|`unnest`|✓|✗|✓|✗|

`regex` additionally gates the operator list: below PostgreSQL 15, and
on live D1, the palette offers 13 operators, not 15 — `~`/`!~` are gone,
not broken.

`LIKE` case sensitivity is another seam: ibis's own SQLite connections set
`PRAGMA case_sensitive_like = ON` at connect and DuckDB's `LIKE` is
case-sensitive, but live D1 is stateless HTTP — no pragma can ride it — so
there `LIKE` keeps stock SQLite's case-insensitive default (`kind like 'c%'`
matches `CLICK`). `ilike` is unaffected (ibis lowers it to `LOWER … LIKE`).
Both the drift and the `translate`/`~`/`!~`/`url_*` refusals are pinned in
`tests/test_backend_matrix.py`, which runs the whole parity matrix against a
faked D1 API whose far end is a real UDF-less SQLite engine.

### 4.5 How this is reproduced

```bash
uv run python probe_functions.py > probe_matrix.json
```

Compiles one representative ibis call per catalog function against all
19 compilable dialects (`ibis.to_sql`, offline) and executes it on each
live connection — demo DuckDB, the D1 snapshot, and PostgreSQL when
`D8R_TEST_POSTGRES=1` with `PGHOST`/`PGPORT`/`PGDATABASE`/`PGUSER`/
`PGPASSWORD` set (the compose service: `docker compose up -d`,
port 55433). The app's own answers come from the same mechanism at
runtime (`_probe_flags` / `_unrenderable_on` / the `pg_proc` check), so
palette and matrix cannot drift apart.
