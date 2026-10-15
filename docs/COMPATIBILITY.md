# D8R — sectors, functionality, function compatibility

The application-wide map: what each sector does, what the language can
express, and where compiler support differs from execution support.

The tables summarize representative probes against ibis 12.0.0 and the
capabilities advertised by `capabilities_for` (`d8r/engine/datasources.py`).
Compiler probes establish rendering support, not successful execution on a
vendor server. The offline tests execute local DuckDB and SQLite; D1 HTTP
transport is mocked over a local SQLite engine. PostgreSQL integration is
opt-in and was not exercised for this alpha. Generic adapters are not
live-certified. See [public alpha evidence and limitations](ALPHA.md).

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
|`datasources.py`|The registry: `demo` (Parquet on DuckDB), four vendor mocks, runtime-added SQLite snapshots (`d1`), Cloudflare D1 (`d1-live`), PostgreSQL (`postgres-live`), local Parquet and generic Ibis sources. `DIALECTS` lists 20 compile targets. `capabilities_for` combines compiler probes, source-specific restrictions, and PostgreSQL extension discovery; these are not exhaustive execution tests.|
|`connectors.py`|Real `kind="ibis"` adapters for every advertised backend except the dedicated PostgreSQL path; backend form metadata, profile normalization/identity, safe credential handling, namespace-only discovery and owned-resource disposal. Optional drivers and native runtimes remain explicit prerequisites; adapter availability is not live-integration certification.|
|`expression.py`|`build` — payload → `ir.Table`: select items (column / literal / aggregate / temporal / scalar call / regex / rank / window / case / subquery), the recursive `_condition_tree` for `\where` (ands, or-groups, `( … )` groups), joins incl. the lateral correlation hoisting + top-N rewrite, set ops, `compile_sql` (render-only, subject to dialect and expression support).|
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

The scalar families covered by the tables below. Arity and argument kinds are
enforced by the parser (`d8r/query/functions.py`); result kinds are ibis's.
This is not an exhaustive compatibility matrix for every language feature.

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

Two independent questions:

* **Compile** — `ctrl+k` renders supported expressions without executing the
  query. There are 19 offline dialect targets; the 20th, PySpark, requires
  its real Spark runtime. A rendering rule for one function does not mean
  every document, argument type or combination compiles for that target.
* **Execute** — Run uses the active source, not the selected rendering
  dialect. A rendered expression can still fail because of server versions,
  missing extensions/UDFs, types, permissions or runtime prerequisites.
  `capabilities_for` filters known gaps; it is not a live certification suite.

The matrices below separate local execution, mocked transport, optional
integration coverage and compiler-only evidence. Generic sources probe their
compilers offline and enable transactions/temp tables only for the existing
DuckDB/SQLite implementations. Connection setup and native prerequisites are
documented in the [user guide](USER_GUIDE.md#add-trino-or-another-ibis-database).

### 4.1 Common functions are not universal guarantees

Aggregates, temporal extraction, ranks, cross-type functions and many string
functions have broad compiler support. They are not guaranteed to execute on
every advertised backend. For example, `concat_ws` has an Exasol rendering
gap, while D1 excludes `reverse`, `repeat`, `lpad` and `rpad` because their
SQLite translations require UDFs unavailable to its remote execution path.
Check the target-specific gaps below and test the actual query on the intended
server before relying on it.

### 4.2 Compile matrix — per-dialect gaps

Representative scalar calls have the following recorded rendering gaps against
ibis 12.0.0 (`·`-listed = cannot render). This is compiler evidence only, not
proof of arbitrary-query support or successful vendor-server execution.

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
|`pyspark`|Requires its real live Spark session; unavailable for offline compilation.|

Notes:

* `convert_base` has an offline rendering rule only for Impala in this matrix.
  The local execution baseline cannot run it; live Impala was not exercised.
* The SQLite compile gaps are about the *plain* dialect; the app's local
  snapshot connection closes some of them with registered UDFs (§4.3).

### 4.3 Execution matrix — local baseline and integration expectations

`✓` describes advertised support for the listed family, subject to types and
runtime prerequisites; it does not certify every call. Local DuckDB/SQLite
have executable coverage in `tests/test_backend_matrix.py`. **The D1 column
uses `httpx.MockTransport` with real UDF-less local SQLite, not Cloudflare.**
**The PostgreSQL column describes opt-in integration expectations; those tests
were not run for this alpha.** No generic vendor connector is certified here.

|Function family|demo + mocks<br>(local DuckDB)|SQLite snapshot<br>(local `kind="d1"`)|PostgreSQL<br>(opt-in; not exercised)|D1 path<br>(mocked HTTP / local SQLite)|
|---|---|---|---|---|
|5 aggregates, 7 temporal, 3 ranks, cross-type, string family excluding the rows below|✓|✓|✓|✓|
|`reverse` / `repeat` / `lpad` / `rpad`|✓|✓|✓|✗ (UDF-dependent)|
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

`supports` as advertised by `capabilities_for`, using compiler probes and
source-specific rules. These flags are not evidence that every operation has
run successfully on a live server. PostgreSQL remains opt-in/unexercised in
this alpha; D1 evidence remains mocked HTTP over local SQLite, as in §4.3.

|Flag|demo / mocks<br>(local DuckDB)|SQLite snapshot<br>(local)|PostgreSQL<br>(opt-in; not exercised)|D1 path<br>(mocked HTTP / local SQLite)|
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

Compiles one representative ibis call per catalog function against the
19 offline dialects (`ibis.to_sql`) and executes against demo DuckDB and the
local SQLite snapshot. PostgreSQL is added only with `D8R_TEST_POSTGRES=1`
and `PGHOST`/`PGPORT`/`PGDATABASE`/`PGUSER`/`PGPASSWORD` configured. Follow the
canonical [development PostgreSQL setup](USER_GUIDE.md#local-postgresql-with-docker)
for the gitignored password file, explicit Compose environment file and
disposable integration database; a bare `docker compose up` is insufficient.

This command is a reproduction procedure, not a claim that all optional
integrations were run for the alpha. Runtime capability filtering uses
compiler probes and the PostgreSQL `pg_proc` check; compare actual query
results too, because advertised capabilities and execution can differ.
