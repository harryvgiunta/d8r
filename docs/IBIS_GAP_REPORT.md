# ibis 12.0.0 capabilities missing from D8R

Gap report: what the installed **ibis-framework 12.0.0** can do that D8R's
document language, engine payload, and TUI never reach. Every "missing" claim
was grounded by reading the D8R code that would have to carry the feature and
by checking that no payload key, parser command, or engine map gets there;
every "ibis has it" claim was grounded by introspecting the installed package,
not by memory. Items that are honest gaps and items that collide with an
AGENTS.md hard boundary are kept separate: gaps are recommended below,
boundaries are listed in [Deliberate boundaries](#deliberate-boundaries) with
no recommendation.

## Method

**Introspected environment.** `.venv/Scripts/python` with
`ibis.__version__ == '12.0.0'` installed (verified by running it). The surface
was walked live, e.g.:

```python
import importlib.metadata as md
sorted(ep.name for ep in md.entry_points(group="ibis.backends"))
# -> 22 backends: athena bigquery clickhouse databricks datafusion druid duckdb
#    exasol flink impala materialize mssql mysql oracle polars postgres pyspark
#    risingwave singlestoredb snowflake sqlite trino

from ibis.expr.types.strings import StringValue; sorted(dir(StringValue))   # string family walk
from ibis.expr.types.numeric import NumericColumn; sorted(dir(NumericColumn))  # aggregate family walk
ibis.to_sql(expr, dialect=d)          # per-dialect compile probes (all 22 entry-point names)
con.has_operation(ops.Quantile)       # True on the live duckdb connection
con.execute(expr, params={p: 1})      # ibis.param end-to-end probe
```

Backend reachability was tested by compiling real expressions per dialect
(including D8R-shaped payloads built through `expression.build`, an
aggregate+order+limit query and a windowed `rank()`/frame query), and the
nested/JSON/array claims were compiled against a live in-process DuckDB table
created with `json`, `int[]`, `map(varchar,int)`, and `struct(a int, b
varchar)` columns. D8R grammar claims (`\where a = 1 and b = 2`,
`\where x is null`, `\where amount between 1 and 2`, `\order x desc nulls
first`, `\limit 5 offset 2`, `\having`, `\select amount * 2`, `\left join`,
`\where x in (1, 2)`, `\sample`, `percent_rank() over (…)`,
`range between 1 day preceding and current row`) were each run through
`parse_query(..., settled=True)` and, where they parsed, through
`payload_from_ast` + `expression.build` to see what actually reaches ibis.

**D8R files read.** `d8r/query/parser.py` (grammar, `payload_from_ast`,
function-call machinery), `d8r/query/functions.py` (the scalar catalog),
`d8r/query/schema.py` (`SchemaContext`, `Capabilities`, `dtype_family`),
`docs/AST.md` (the language contract), `d8r/engine/expression.py` (the whole
payload→ibis builder: `AGGREGATE_FNS`, `TEMPORAL_FNS`, `OPERATORS`,
`RANK_FNS`, `SET_OP_METHODS`, `_scalar_call`, `_window_frame`, `_join_frames`,
`_regex_column`, `build`), `d8r/engine/execute.py`, `d8r/engine/datasources.py`
(`CAPABILITIES`, `capabilities_for`, `DIALECTS`, ingestion, postgres source
builder), `d8r/engine/tx.py` (the only write path), `d8r/tui/palette.py`
(`COMMANDS`, operator/aggregate offers), `d8r/tui/session.py` (`refresh_schema`,
`default_rows` limit injection, `column_values` pool), plus `AGENTS.md`.

**Baseline for "D8R status".** The payload has exactly the keys
`dataset, alias, body, joins, setOps, select, distinct, where, groupBy, orderBy,
limit, cases, temp, drop, tx, ctes` (`payload_from_ast`,
`d8r/query/parser.py:1674`). `\where` operators are
`= != > >= < <= like ~ !~ in "not in"` (`OPERATORS`, `d8r/engine/expression.py:33`);
aggregates are `sum avg count min max` (`AGGREGATE_FNS:27`); ranks are
`rank dense_rank row_number` (`RANK_FNS:499`); temporal calls are the seven
extraction functions (`TEMPORAL_FNS:36`); scalar calls are the 22-entry string
catalog in `d8r/query/functions.py`. Dtype families recognized for completion
are `timestamp/date/time/string` — everything else is `None`
(`dtype_family`, `d8r/query/schema.py:73`).

## Summary

Ranked by (value to the document author × backend reality on D8R's live
sources — DuckDB demo, SQLite D1, live PostgreSQL):

1. **Scalar expressions: numeric ops + arithmetic + richer string/temporal
   functions.** The biggest hole by far. `\select amount * 2` does not parse;
   `round`, `abs`, `ifelse`, `contains`-family beyond the fixed catalog,
   `trunc`/`strftime`/date arithmetic — all exist in ibis, all compile on every
   live source, and the engine's `_scalar_call` already dispatches generically
   (`getattr(first, fn)(*rest)`, `d8r/engine/expression.py`), so most of
   this is catalog + parser-signature work, not new machinery. (`coalesce`/
   `nullif` since shipped — see Conditional / structural.)
2. **`having`** — ibis `aggregate(..., having=)` / `GroupedTable.agg(having=)`;
   `\having` is currently "unknown command". A grouped query cannot be filtered
   today.
3. **Ordering: `nulls first/last`, random order, `offset`.** One-argument
   pass-throughs into `asc(nulls_first=)`, `ibis.random()`, `limit(n, offset=)`,
   all verified to compile for every D8R dialect that D8R claims.
4. **Aggregate breadth: `count_distinct`, `var`/`std`, `median`, `quantile`,
   `approx_*`, `arbitrary`, `first`/`last`, `group_concat`, filtered
   aggregates (`where=`)** — one table (`AGGREGATE_FNS`) plus the
   `CAPABILITIES["aggregates"]` list; the palette and completion already read
   that list.
5. **Window functions: `lag/lead`, `percent_rank`, `cume_dist`, `ntile`,
   cumulative `cumsum`-style, `first_value`/`last_value`/`nth_value`** — D8R's
   `over (…)` accepts only the 3 ranks and the 5 aggregates.
6. **`table.sample(fraction, seed=…)`** — real sampling (DuckDB/PostgreSQL
   support the seeded form; the AI "sample_rows" tool today is an unordered
   `LIMIT`, not a sample).
7. **Join kinds** — ibis `JoinKind` is
   `inner|left|right|outer|asof|semi|anti|any_inner|any_left|cross|positional`
   (`ibis/expr/operations/relations.py:201`); D8R reaches inner-equality plus
   lateral, and semi/anti only through `in`/`not in`.
8. **Table profiling in the schema pane** — `Table.value_counts()`,
   `Column.topk`, `Table.info`, `Column.histogram` over the live connection;
   natural fit for the existing Schema pane.
9. **Export via ibis (`to_parquet`/`to_xlsx`/`to_csv`)** for the results pane
   (CSV already exists in the TUI via the `csv` module; Parquet/XLSX do not).
10. **Typed runtime parameters (`ibis.param`)** — end-to-end verified on the
    live connection; would make `\fn` arguments real values instead of textual
    splice-in.

D8R's **dialect coverage is essentially complete** (see
[Dialect coverage](#dialect-and-backend-coverage)); no work recommended there
beyond two footnotes.

## Scalar operations by dtype family

D8R's entire scalar surface is: the 22 string functions in
`SCALAR_FUNCTIONS` (`d8r/query/functions.py:28`), mapped by `_scalar_call`
(`d8r/engine/expression.py:708`) which dispatches
`getattr(first, fn)(*rest)` after per-argument kind checks (`string`/`integer`
only); the seven `TEMPORAL_FNS` extractions; `string(x)` (a cast); and the two
regex select items (`regexp_extract`/`regexp_replace`, `_regex_column:661`).
There are **no numeric functions, no arithmetic, no conditional functions** —
verified: `\select amount * 2` → `cannot parse expression "amount * 2"`.

### String

| capability | ibis surface | D8R status | where it would hook in | effort | recommendation |
| --- | --- | --- | --- | --- | --- |
| SQL pattern match / case-insensitive / POSIX | `StringValue.like(patterns)` (accepts several patterns), `.ilike(…)`, `.rlike(…)` | **partial → deviating**: `\where … like "%a%"` compiles to `column.contains(str(value).strip("%"))` (`_predicate`, `expression.py:383`) — a substring match, not SQL `LIKE` semantics; `ilike`/`rlike` not offered at all (`\where x ilike "…"` refuses) | add ops to `COMPARISONS`/`OPERATORS` + one branch in `_predicate`; `CAPABILITIES["operators"]` per source | S | Add `ilike` (maps to `.ilike`) at minimum; document `like`'s containment semantics or switch it to `.like` behind a capability flag |
| regex search as predicate | `re_search(pattern)` | **exposed** as `~`/`!~` (`_regex_match:393`) | — | — | fine as is |
| regex split / group extraction | `re_split(delim)` → array; `re_extract(pattern, index)` | **partial**: `regexp_extract` exists as a select item; `re_split` produces an array nothing in the language can consume | new `split` entry in `SCALAR_FUNCTIONS` only once unnest exists (below) | M | defer until nested/array support |
| find / position | `find(sub, start, end)` | **exposed** (`find`) | — | — | fine |
| pad / trim / case / slice / concat | `lpad/rpad/strip/lstrip/rstrip/lower/upper/capitalize/substr/left/right/repeat/reverse/replace/contains/startswith/endswith/length/concat/concat_ws` | **exposed** (all in `SCALAR_FUNCTIONS`) | — | — | fine |
| translate / levenshtein / ascii / hex | `translate(from, to)`, `levenshtein(other)`, `ascii_str`, `hexdigest`, `hashbytes`, `convert_base` | **missing** | new `SCALAR_FUNCTIONS` entries + signature rows; `_scalar_call` already dispatches generically | S | add `translate`, `levenshtein` (both compile on duckdb/postgres; check sqlite per op) |
| URL field accessors | `url`-family: `protocol/host/path/query/fragment/userinfo/authority/file` on `StringValue` | **missing** | same table | S | nice palette demo; duckdb/postgres support varies — gate via capabilities |
| string → date/time cast | `as_date`, `as_time`, `as_timestamp` | **missing** (only `string(x)` cast to string exists) | `SCALAR_FUNCTIONS` entry with result-kind `timestamp`; needs `ResultKind` extension in `functions.py` | S | add `as_date`/`as_timestamp` |
| int → string bases | `IntegerValue.convert_base(from_base)`, `StringValue.convert_base` | **missing** | as above | S | optional |

### Numeric (entirely absent — biggest single gap)

| capability | ibis surface | D8R status | where it would hook in | effort | recommendation |
| --- | --- | --- | --- | --- | --- |
| arithmetic | `+ - * / // % **` operators on `NumericValue` (`add/sub/mul/div/floordiv/mod/pow`) | **missing** — verified `cannot parse expression "amount * 2"`; payload `select` items carry no expression node | a real expression grammar in `parser.py` (binary ops, precedence, parentheses over columns/literals/function calls), a `expr` field on select items + `where` operands, and an evaluator in `expression.py` that folds the AST node tree onto ibis operators | **L** | the marquee item: it also unlocks conditional ops, richer case branches, and `\order amount * 2`; design the AST node shape first |
| rounding / clipping / sign | `round(n)`, `ceil`, `floor`, `abs`, `sign`, `clip(lower, upper)`, `negate` | **missing** | `functions.py` entries (needs `"numeric"` argument kinds — `ArgumentKind` today is `string/integer/any`) + `_scalar_call` (dispatch already generic) | S | add `abs/round/ceil/floor/clip` |
| powers / logs | `sqrt`, `exp`, `ln`, `log(x)`, `log2`, `log10`, `pow`, and `log(b, x)` | **missing** | same | S | add `sqrt/exp/log/log10/pow` |
| trig | `sin cos tan cot asin acos atan atan2 degrees radians` | **missing** | same | S | low priority |
| int → timestamp / interval | `IntegerValue.as_timestamp(unit)`, `as_interval(unit)` | **missing** | `TEMPORAL_FNS`-adjacent | M | pair with interval arithmetic |

### Temporal

| capability | ibis surface | D8R status | where it would hook in | effort | recommendation |
| --- | --- | --- | --- | --- | --- |
| date-part extraction | `.year .month .day .quarter .hour .minute .second` (+ `.iso_year .week_of_year .day_of_week .day_of_year .microsecond .millisecond .epoch_seconds .epoch_days`) | **partial**: the 7 in `TEMPORAL_FNS` are exposed; the other ~10 exist on `TimestampValue`/`DateValue` (verified family walk) and reach nothing | extend `TEMPORAL_FNS` + `CAPABILITIES["functions"]` per dtype family + palette offers (it already reads this map) | S | add `week_of_year`, `day_of_week`, `day_of_year`, `iso_year`, `epoch_seconds` |
| truncate / bucket | `truncate("month")`, `bucket(interval)` (verified to compile on duckdb: `RANGE BETWEEN INTERVAL …`) | **missing** | `TEMPORAL_FNS` entry with a unit-argument grammar (`trunc(timestamp, "month")`) — payload `temporal.fn`/`temporal.arg` already carry one arg | M | this is the standard time-series grouping; highest-value temporal add |
| date arithmetic | `timestamp ± interval` (`ibis.interval(days=…)`, `.delta(other)` → interval, `IntervalValue.to_unit`, `.days/.hours/…`) | **missing** — no interval literals anywhere in the language; frame bounds reject them too (below) | interval literal syntax (`1 day`) in the value lexer + arithmetic grammar | L | land with the expression grammar |
| formatting / parsing | `strftime(fmt)`, `StringValue.as_timestamp` | **missing** | `TEMPORAL_FNS` / `SCALAR_FUNCTIONS` | S | add `strftime` |
| timestamp→date / time | `.date`, `.time` | **missing** | as above | S | cheap |

### Conditional / structural

| capability | ibis surface | D8R status | where it would hook in | effort | recommendation |
| --- | --- | --- | --- | --- | --- |
| coalesce / nullif | `ibis.coalesce(a, b, …)` (variadic), `Value.nullif(b)` | **done**: `coalesce`/`nullif` are in `SCALAR_FUNCTIONS` (cross-type, variadic `coalesce`), mapped in `expression.py` and advertised under the `any` dtype family for every backend | — | — | shipped |
| if/then on booleans | `BooleanValue.ifelse(true_expr, false_expr)` | **missing** | expression grammar + `_scalar_call` | M | with arithmetic |
| simple CASE | `Value.case((when, then), …, else_=…)` / `ibis.switch` | **partial**: `\case` is searched-only with fixed shape `<alias> = when <col> <op> <value> then <value> [else <value>]` and `then`/`else` must be literals; branches can't reference other columns or expressions | `cases` payload entry widened to full predicates/expressions in `payload_from_ast` + `_apply` in `expression.py` | M | widen after arithmetic |
| boolean composition | `filter(pred1, pred2, …)` ANDs; `\|`/`&`/`~` operators | **done**: one `\where` line is an `or`-of-`and` tree (`or` looser), parsed by `split_logic` (masking quotes, parens, and `between`'s `and`), mapped to a `where` payload with `ands`/`ors`, and built with `functools.reduce(operator.or_, …)` over `and`-reduced groups. Precedence is verified against a pandas mask in `tests/test_where_predicates.py` | — | — | shipped |
| `is null` / `between` / `in <literal list>` | `isnull()`, `between(lo, hi)`, `isin([...])` | **done**: `is null`/`is not null` (no operand), `between <low> and <high>` (inclusive, `low`/`high`), and `in`/`not in` over a literal `( a, b )` **or** an inline subquery, each mapped in `_predicate`; the old "no bare value list" grammar decision is lifted. Rows verified against pandas masks; a wrong-type list value is the engine's `cannot compare numeric column …` | — | — | shipped |
| substitution of values | `Value.substitute({a: b}, else=…)` | **missing** | `SCALAR_FUNCTIONS`-style entry | S | optional |

## Aggregate and statistical functions

`AGGREGATE_FNS = {"sum","avg","count","min","max"}`
(`d8r/engine/expression.py:27`) mirrored into
`CAPABILITIES["aggregates"]` (`d8r/engine/datasources.py:53`); the palette's
function offer comes from `_aggregate_fns` intersected with what the backend
advertises (`d8r/tui/palette.py:316`). Verified refusals: `\select var(amount)`
→ `unknown function "var"`, `count_distinct(user_id)` likewise. Every ibis
reduction also accepts a `where=` mask (`Filterable` in
`ibis/expr/operations/reductions.py`; verified `t.v.sum(where=t.g)` executes)
— D8R has no filtered-aggregate syntax at all.

| capability | ibis surface | D8R status | where it would hook in | effort | recommendation |
| --- | --- | --- | --- | --- | --- |
| distinct counts | `col.nunique()`; ops `CountDistinct`, `ApproxCountDistinct`, `approx_count_distinct()` | **missing** | `AGGREGATE_FNS` + `CAPABILITIES["aggregates"]` + `expression._aggregate` branch (`getattr`-style dispatch fits) | S | add `count_distinct` first — it's the aggregate users reach for after `count` |
| variance / std dev | `col.var(how='sample'\|'pop')`, `col.std(how=…)` | **missing** | as above; `how=` needs an extra aggregate arg or two names (`var_pop`) | S | add `var`/`std` (duckdb/postgres yes; sqlite: no `var` rule — verified `describe` fails on sqlite via its Quantile/Mode rules) |
| median / quantile | `col.median()`, `col.quantile(q)`, `approx_quantile`, `approx_median`, `approx_nunique` | **missing** | as above; **capability-gate** — `OperationNotDefinedError: 'Quantile'` verified on sqlite and athena; `con.has_operation(ops.Quantile)` is `True` on the live duckdb connection, so the capability dict could stop being static | M | add `median`+`quantile` behind a per-backend flag |
| mode / skew / kurtosis | `col.mode()`, `col.skew()`, `col.kurtosis()` | **missing** | same table | M | low priority; `Mode`/`Kurtosis` rules absent on several dialects |
| first / last / arbitrary | `col.first()`, `col.last()`, `col.arbitrary()` | **missing** as aggregates (present only as window functions in ibis) | same table | S | add `arbitrary` |
| argmax / argmin | `col.argmax(key)`, `col.argmin(key)` | **missing** | two-arg aggregate — `_aggregate` currently assumes `arg` is one column | M | optional |
| list / string collect | `col.collect()` (array), `col.group_concat(sep)` | **missing** | same table; `collect` needs the array story to be useful, `group_concat` is standalone | S (group_concat) | add `group_concat` |
| all / any | `col.all()`, `col.any()`, `notall`, `notany` | **missing** | same table | S | cheap on postgres/duckdb |
| filtered aggregates | every reduction's `where=` keyword | **missing** | aggregate spec gains `{fn, arg, where: {column, op, value}}`; `_aggregate` passes `where=_predicate(...)` — the predicate machinery already exists and is shared with case-`when` | M | clean, high value for `count(where=…)` ratio queries |
| HAVING | `Table.aggregate(metrics, by=…, having=…)` / `GroupedTable.agg(…, having=…)` — in this build it renders as an outer `WHERE` over the grouped subquery (verified) | **missing**: `\having …` → `unknown command "\having"` | new `\having <col> <op> <value>` command + payload key; `expression.build` filters after `.aggregate(...)` | M | top-3 item: grouped queries are unusable without it today |

## Window functions and frames

D8R's `over ( … )` (`_window_frame`, `d8r/engine/expression.py:524`) accepts
exactly: `partition by <cols>`, one `order by <col> [asc|desc]`, and a frame of
`rows|range between <bound> and <bound>` where a bound is
`unbounded preceding|<n> preceding|current row|<n> following|unbounded
following` (`_frame_offset:506` maps these to ibis' signed-offset convention).
The windowed item itself is either a rank from `RANK_FNS = {rank, dense_rank,
row_number}` (+1, because ibis dialects render 0-based — verified in the
compiled SQL) or one of the five capability aggregates over the frame.
Verified: `percent_rank() over (…)` → `cannot parse expression "percent_rank()"`.

| capability | ibis surface | D8R status | where it would hook in | effort | recommendation |
| --- | --- | --- | --- | --- | --- |
| rank family breadth | top-level `ibis.percent_rank()`, `ibis.cume_dist()`, `ibis.ntile(buckets)`, `ibis.rank()`, `ibis.dense_rank()`, `ibis.row_number()`; `col.min_rank` absent in 12 | **missing** (3 of 6) | `RANK_FNS` + `CAPABILITIES["windowFunctions"]` + the parser's rank-call name check (it already parses `<fn>() over ( … )` syntactically — `percent_rank()` only died on the catalog, verified) | S | add `percent_rank`, `cume_dist`; `ntile(n)` takes an argument, so the rank node needs an `arg` (payload `rank: {fn, arg}`) — M |
| lag / lead | `col.lag(offset, default)`, `col.lead(…)`; `col.first()/.last()/.nth(n)` as window fns | **missing** | a new windowed-item shape `{window, aggregate:{fn,arg}}` extended with `lag/lead` and an `offset` arg — `_windowed_column`'s aggregate branch generalizes with an `offset`-aware dispatch | M | **top window gap** — trend comparisons are the classic use |
| first/last/nth value | window `first`/`last`/`nth` over a frame | **missing** | same mechanism | M | after lag/lead |
| cumulative shortcuts | `col.cumsum()`, `cummean`, `cummin`, `cummax` (+ `cumall`/`cumany` for booleans) — pre-built windows | **partial equivalent**: `sum(amount) over (order by ts rows unbounded preceding)` already expresses it | palette-only convenience | S | skip; D8R's frame syntax is the better spelling |
| `rows`/`range` with **interval** bounds | `ibis.window(range=(-ibis.interval(days=1), 0))` renders `RANGE BETWEEN INTERVAL '…' DAY …` on duckdb (verified) | **missing** — parser frame bounds are integer-or-unbounded only; verified `range between 1 day preceding and current row` refused with the integer-bounds message | new bound shape in `_FRAME_BOUND_RE`/`_frame_offset`; payload `frame.start/end` stay strings; needs interval literals to be lexable | M | the honest version of time-windowed aggregation until tumble/hop exists |
| frame exclusion (`EXCLUDE CURRENT ROW` etc.) | **does not exist in ibis 12** — `ibis.expr.operations.window.Window` has no exclusion field (verified via its `argnames`) | n/a | — | — | not a D8R gap |
| auto frame `between=(x, y)` / `rows_window` / `trailing_window` | `ibis.window(between=…)`, `ibis.rows_window(preceding, following, …)`, `ibis.trailing_window(preceding, …)` (all present in 12) | equivalent reachable via existing `rows between` | — | — | not a gap |
| time-bucketed windows (`tumble`/`hop`) | `Table.window_by(col)` + `.tumble(size=…)`/`.hop(size=, slide=)`/`ibis.watermark(col, allowed_delay)` | **missing** — and barely reachable: `WindowAggregate` compiles **only** for the flink dialect among the 21 (verified OperationNotDefinedError on duckdb/postgres/clickhouse/materialize/risingwave) | would be a `\window` block with new payload keys | L | **not recommended** — the D8R-relevant backends cannot render it |
| window order direction / multi-key order | window `order_by` accepts multiple sort keys | **partial**: D8R allows exactly one window order key (`(key,) = expr.bind(...)` asserts one) | widen payload `window.order` to a list | M | rare; low priority |

## Joins

`_join_frames` (`d8r/engine/expression.py:267`) is the whole story: `\join
<ds> [as a] on <col>[ = <col>]` → `expr.join(right, left_column ==
right_column)` (line 305/326) — always **inner**, exactly **one equality**
(a multi-predicate `on a = b and c = d` refuses at parse, verified), plus the
lateral rewrite (`_apply_lateral_rows`, which numbers rows and re-projects).
Verified `\left join …` → `unknown command "\left"`. `in`/`not in` subqueries
reach semi/anti semantics through `_apply_subquery_filter` — that is the only
non-inner reachability.

| capability | ibis surface | D8R status | where it would hook in | effort | recommendation |
| --- | --- | --- | --- | --- | --- |
| join flavors | `Table.join(right, preds, how=JoinKind)` with `how` in `inner left right outer semi anti asof cross any_inner any_left positional`; shorthands `left_join/right_join/outer_join/semi_join/anti_join/cross_join` | **missing** (inner only) | payload `joins[].how`; `\join left <ds> …` / `\left join` grammar (the palette offers dataset names after `\join` today — the modifier slot is free); `expression._join_frames` gains `how=` | S–M | add `left`/`right`/`full` first (SQL parity users expect them); sqlite/duckdb/postgres all render them |
| multi-key ON | join predicates are a sequence: `.join(r, [a==x, b==y])` | **missing** (single equality by grammar) | `on` tail becomes a comma-separated list; payload `left/right` → list of pairs | M | common; pairs-list shape keeps the "both sides named" rule |
| arbitrary ON predicate | predicates accept any boolean expression (`a > b`, functions) | **missing** (`on a = b` literally) | `on` tail carries a predicate like `\where`'s | M | after arithmetic lands |
| ASOF join | `Table.asof_join(right, on=, predicates=, tolerance=)` — compiles on duckdb and postgres; **not** sqlite (`UnsupportedOperationError: ASOF joins are not supported by SQLite`, verified) or databricks | **missing** | new `how=asof` with a `>=` time condition + optional `tolerance` | M | flag via `CAPABILITIES["supports"]` per backend, exactly how `savepoints` is gated today |
| CROSS join | `Table.cross_join(right)` | **missing** (`on` is mandatory) | `how=cross` with the `on` clause optional | S | cheap |
| semi / anti as first-class | `semi_join`, `anti_join`, `any_left_join` | **partial**: reachable only as `in`/`not in` subquery filters | `how=semi/anti` for the join-shaped case | S | document the `in`/`not in` path; add kinds if `\join left` ships |
| positional join | `positional` kind (column-position concatenation) | **missing** | payload pass-through | S | skip — no natural `\join` spelling |
| lname/rname suffixes | `.join(..., lname="{name}_", rname="{name}_right")` | **equivalent present**: `_join_frames` keeps first occurrence and renames duplicates (the `_LATERAL_ROW`-clash guard shows it) | — | — | not a gap |

## Set operations

`SET_OP_METHODS = {"union": "union", "intersect": "intersect", "except":
"difference"}` (`expression.py:503`); `\union all`/`\union distinct` set the
`distinct` flag per operation, which D8R passes explicitly to
`getattr(expr, method)(right, distinct=distinct)` (line 657) — necessary
because ibis' defaults differ per method (`union(distinct=False)`,
`intersect/difference(distinct=True)` — verified signatures). Left-deep
application with operand projection to the left's columns is D8R's own
improvement over raw ibis.

| capability | ibis surface | D8R status | where it would hook in | effort | recommendation |
| --- | --- | --- | --- | --- | --- |
| union all / distinct | `union(right, distinct=bool)` | **exposed** (`\union [all\|distinct]`, verified parse) | — | — | fine |
| all three ops + explicit dedup | `union`/`intersect`/`difference` each with `distinct=` | **exposed** (`\union`/`\intersect`/`\except`) | — | — | fine |
| multi-operand in one call | `union(t2, t3, …)` variadic | **equivalent**: D8R applies left-deep across multiple clauses — same semantics | — | — | not a gap |
| align-by-position instead of by-name | n/a in ibis (name-aligned) | D8R's projection-to-left-columns is strictly kinder | — | — | fine |

Set ops are the one family with **no material gap**. Note only that
`intersect`/`except` always dedupe (ibis has no distinct-False form on them
beyond the kwarg D8R already threads; sqlite lacks `INTERSECT ALL`-style forms
entirely).

## Ordering and row selection

`orderBy` items are `{target, direction}` only (`payload_from_ast`), resolved
alias-first then by bare column, direction `asc|desc` (`build`,
`expression.py:1018-1033`); `limit` is a plain non-negative int (`expr.limit`).
Verified: `\order amount desc nulls first` → parse error; `\limit 5 offset 2` →
`limit must be a non-negative integer`; there is no `\offset` command.

| capability | ibis surface | D8R status | where it would hook in | effort | recommendation |
| --- | --- | --- | --- | --- | --- |
| NULL placement | `asc(nulls_first=)`, `desc(nulls_first=)`, `ibis.desc("col", nulls_first=True)` — verified render (`ORDER BY … DESC NULLS FIRST`) | **missing** | `orderBy[] += nullsFirst`; `\order <t> [asc\|desc] [nulls first\|last]`; one kwarg in `build` | S | add — top-3 item; every dialect D8R claims renders it |
| random ordering | `order_by(ibis.random())` — verified `ORDER BY RANDOM() ASC` on duckdb | **missing** | `\order random()` special-cased to `sorts.append(ibis.random())` | S | cheap (and honest via capabilities) |
| OFFSET / rows-from | `Table.limit(n, offset=)` — verified `LIMIT 2 OFFSET 1`; also `Expr.execute(limit=…)` and `ibis.options.sql.default_limit` | **missing** — D8R injects `default_rows` as a `limit` in `session.py:438` but pagination is impossible | payload `offset`; `\limit <n> offset <m>` grammar; `build`: `expr.limit(limit, offset=)` | S | add — pagination needs it |
| expression ordering | `order_by(expr)` with any sort key | **missing** (targets are column/alias names; ordering by a computed column already works via its alias) | expression grammar | — | covered by the alias trick today |
| `head`/`preview` | `Table.head(n)`; `Table.preview` (client-side rich preview) | equivalent for `head` via `\limit`; `preview` needs a live con and belongs to no pane | — | — | not a gap |

## Table reshaping: unnest, pivot, counts

| capability | ibis surface | D8R status | where it would hook in | effort | recommendation |
| --- | --- | --- | --- | --- | --- |
| explode a column | `Table.unnest(col, offset=, keep_empty=)`; array `col.unnest()` — verified `CROSS JOIN UNNEST` renders on duckdb (mysql/druid/flink lack the rule) | **missing**; no nested column in any bundled dataset anyway (demo parquet schemas are flat: int64/string/double/timestamp — verified with `pq.read_schema`) | `\select unnest(tags)` node or `\unnest <col>` clause + payload key + `build` step after projection | M | reachable via live PostgreSQL (arrays exist there); ship with the nested section |
| long→wide / wide→long | `pivot_wider(id_cols, names_from, values_from)` (needs a bound backend — it calls `distinct().execute()` internally to learn names, verified) and `pivot_longer(col, names_to, values_to, names_pattern)` | **missing** | a `\pivot` command whose build path must use `con.execute` for names — meaning it is **execution-time**, not compile-time; D8R's compile pane could not show it until the pivot is materialized | L | hold: collides with the "SQL pane shows the executed SQL" invariant |
| value frequency | `Table.value_counts(name=)` / `Column.value_counts()` — verified renders a `GROUP BY`/`COUNT` | **missing** as language; trivially expressible as `\select count(*) \group col \order …` | palette/schema-pane offer, not a command | S | expose in the Schema pane profiling instead (below) |
| top-K values | `Column.topk(k, name=)` | **missing** | same | S | same |
| row id / unpack / relocate | `Table.rowid`, `Table.unpack(*struct_cols)`, `relocate`, `rename`, `drop` | **missing** (D8R's empty `\select` is its dedup-star; `rename` is handled by join aliasing) | — | — | low value |

## Nested data (struct / map / array / JSON)

Ibis's typed family APIs (all verified against a live duckdb table with `json`,
`int[]`, `map(varchar,int)`, `struct<a,b>` columns):

| capability | ibis surface | D8R status | where it would hook in | effort | recommendation |
| --- | --- | --- | --- | --- | --- |
| struct field access / explode | `st.field`, `StructValue.names/types/fields/lift`, `Table.unpack("st")` → `st.a AS a, st.b AS b` (verified) | **missing** — `dtype_family` maps struct/json/array/map columns to `None`, so they get zero completion and no ops | `dtype_family` gets families; select items get a `field` node; `ColumnDef.type` already carries the raw ibis dtype name | M | start here if any source ships nested columns |
| array ops | `length/contains/index/slice/map/filter/flatten/sort/unique/union/intersect/zip/join/unnest` + `ibis.array([…])` literal (verified `CAST([1,2,3] AS TINYINT[])`) | **missing** | same | M | after struct |
| map ops | `keys/values/length/get(key, default)/contains` | **missing** | same | M | after struct |
| JSON path access | `data["id"]` → `-> '$.id'`, `JSONValue.int/float/str/bool/array/map/unwrap_as` | **missing** | same | M | PostgreSQL live sources are where JSON columns actually live — the gap that bites first in practice |

Honest caveat: **none of this is reachable through the bundled demo data**
(flat schemas), so the gap's priority rises only for PostgreSQL sources or
user-parquet ingestion (`_ingest` registers any parquet, which *can* carry
nested columns). The dtype-name plumbing (`ColumnDef.type`,
`datasources._describe_table`) is already there; what's missing is the family
map, the ops, and the grammar nodes.

## Parameters (`ibis.param`)

| capability | ibis surface | D8R status | where it would hook in | effort | recommendation |
| --- | --- | --- | --- | --- | --- |
| typed query parameters | `ibis.param("int64")` referenced inside an expression, values supplied at run time — verified end-to-end: `con.execute(t.filter(t.v > p), params={p: 1})` returns filtered rows on the live duckdb connection; `execute`/`to_pandas`/`con.compile` all take `params=` | **missing** — `@name` tokens exist only as **textual** splice-in inside `\fn` bodies (`substitute_params`, `parser.py:479`, "the substitution is textual on purpose: … ibis never learns a function existed"); no payload key carries a parameter | `\param <name> <type>` command; payload `params: [{name, type}]`; `expression.build` threads `ir.Value` params into the tree; `execute.execute`/`compile_sql` gain a `params` dict (the run/compile seam already has one call site each) | L | worth doing for AI tool-calls and saved-query reuse, where text-splice is fragile; keep `\fn` textual bodies as-is |

## Raw SQL passthrough

| capability | ibis surface | D8R status | where it would hook in | effort | recommendation |
| --- | --- | --- | --- | --- | --- |
| SQL string → table expression | `con.sql(query, schema=…)` (verified running on the duckdb connection); `Table.sql(query)` — note this is "**run** a SQL query against a table expression", not a renderer; `ibis.parse_sql(sqlstring, catalog, dialect)` (experimental, sqlglot-based); **`ibis.sql` does not exist at top level in 12.0.0** | **deliberate boundary** — see below | — | — | listed under [Deliberate boundaries](#deliberate-boundaries) |
| expression → SQL rendering | `ibis.to_sql(expr, dialect=…)` | **exposed** — `expression.compile_sql` is exactly this (`expression.py:1086`), powering the SQL pane | — | — | fine |
| expression → Python source | `ibis.decompile(expr, format=False)` (verified emitting readable Python) | **missing** | a "Decompile" palette action rendering the code in a pane — pure display, executes nothing, no boundary collision | S | cheap and genuinely delightful for the IDE story |

## UDFs (Python in expressions)

| capability | ibis surface | D8R status | where it would hook in | effort | recommendation |
| --- | --- | --- | --- | --- | --- |
| Python scalar UDFs | `@ibis.udf.scalar.python` (verified: registers on the in-process duckdb connection, executes, and compiles to a `TAG_0("k")` call); `udf.scalar.pandas`/`.pyarrow`; `udf.scalar.builtin` / `udf.agg.builtin` (name-into-backend). The legacy `elementwise/aggregate/aw` decorator names are gone from 12 (only `ibis.legacy.udf.vectorized.*` remains, deprecated) | **missing** — `\fn` is a textual document-macro, not code; a call inside `\fn` body is substituted text, and per AGENTS.md there is no "run arbitrary code" surface anywhere | would be: `@tool`-style function definitions in the `\fn` editor compiled via `ibis.udf.scalar.python`, gated by a new `supports.pythonUdfs` capability (demo/duckdb **yes** — verified executing; d1-live/postgres **no** — the compiled call names a function the real server doesn't have, exactly the `_IBIS_REGEX_SEARCH` problem `capabilities_for` already reasons about) | L | the boundary here is *unwritten*, but the capability story gets ugly fast; recommended only as a demo-source experiment, keeping `\fn` textual as the default |

## Write-back

| capability | ibis surface | D8R status | where it would hook in | effort | recommendation |
| --- | --- | --- | --- | --- | --- |
| `con.insert(table, obj)` | verified working on the duckdb connection (insert grew the row count) | **deliberate boundary** (only `\temp` writes; AGENTS.md persistence rule) | — | — | see boundaries |
| `con.create_table` / `create_view` / `upsert` / `truncate_table` / `rename_table` | `create_table` **is broken in this pinned environment even for the non-temp path** — re-verified: `ParserException: syntax error at end of input` from the sqlglot-30 `DROP … IF EXISTS` nameless rendering documented in AGENTS.md working notes (`tx.py:13-14`) | **deliberate boundary**, and half-unusable anyway | — | — | see boundaries |
| export: `to_csv`, `to_parquet`, `to_xlsx`, `to_delta`, `to_json`, `to_pandas(_batches)`, `to_pyarrow(_batches)`, `to_polars`, `to_torch` | all present on `Table` (signatures verified; `to_csv`/`to_parquet` take `(path, *, params=…)` and bypass the row JSON round-trip) | **partial**: the Export results action writes CSV through Python's `csv` module over already-rendered rows (`d8r/tui/results.py:327`), not through ibis; Parquet/XLSX don't exist | `session` already holds the built expression per run — a `to_parquet(path)`/`to_xlsx(path)` call on it is the whole change | M | **recommended as an exception to the no-write posture** in the same spirit as CSV export: user-asked file output, outside the persistence boundary's database-state scope (AGENTS.md scopes persistence to database/results state staying in memory) |

## Schema introspection extras

Verified: ibis `Schema` carries **only** name→dtype — no column comments, no
table comments, no foreign keys anywhere: a grep over the entire installed
`ibis/backends` tree for `foreign_key`, `table_comment`, `col_comment` returns
**zero hits**, and `con.get_schema` returns types only. So "doc metadata / FK
via ibis" is not an ibis gap at all; D8R's `TableDef.doc`/`ColumnDef.doc`
fields (`d8r/query/schema.py:27-44`) are its own catalog text and can stay so.

| capability | ibis surface | D8R status | where it would hook in | effort | recommendation |
| --- | --- | --- | --- | --- | --- |
| column profiling | `Column.value_counts()`, `Column.topk(k)`, `Column.histogram(bins)`, `Table.describe()` (compiles on duckdb/postgres/snowflake, fails on sqlite/athena via missing Quantile/Mode rules — verified), `Table.info()` | **missing** — the Schema pane shows names/types/D8R docs; the value pool (`datasources.column_values`, `.distinct()` capped at `VALUE_POOL_LIMIT=1000`) is the only statistics that exist | Schema-pane "profile column": `session` runs `col.value_counts().limit(n)` through the existing execute seam | M | recommended — it's the IDE's "what is in this column" moment |
| table listing / search | `con.list_tables(like=)`, `con.tables` accessor, `con.version`, `db_identity` | **partial**: `list_tables()` is used at source-build time (postgres builder filters `information_schema.tables` directly because `list_tables(public)` leaks temp names — verified comment at `datasources.py:735`); no live search | palette source-scoped table search | S | optional |
| operation capability probe | `con.has_operation(ops.Quantile)` → `True` on duckdb (takes an **operation class**, not a string — verified the string form raises `AttributeError`) | **missing** — `CAPABILITIES` is a static dict per source kind (`datasources.CAPABILITIES`, `capabilities_for`) | for live sources (duckdb demo, d1 snapshot, postgres), build the aggregates/ops flags from `has_operation` at `refresh_schema` time instead of hand-listing | S | recommended — this is what makes every dialect-gated recommendation above honest instead of advertised |

## Execution extras

D8R's seam is `execute.execute`: `build → compile_sql → expr.execute()` →
pandas frame → JSON-safe rows (`d8r/engine/execute.py:37`), plus
`execute_remote` for d1-live.

| capability | ibis surface | D8R status | where it would hook in | effort | recommendation |
| --- | --- | --- | --- | --- | --- |
| server-side row cap | `expr.execute(limit=…)` / `con.execute(expr, limit=…)` / `ibis.options.sql.default_limit` | **missing** — the `limit` payload key (and `Session.default_rows` injection, `session.py:438`) already puts `LIMIT` in the SQL, which is the same thing done earlier and visibly | — | — | not a gap |
| parameterized execution | `execute(params={param: value})` | **missing** (see Parameters) | — | — | — |
| streaming results | `to_pyarrow_batches` / `to_pandas_batches` | **missing**; the results pane buffers everything anyway (`session.py:1030` "shown < total") | only matters if the pane ever virtualizes | L | skip |
| `con.compile(expr, pretty=True)` | pretty via sqlglot, separate from ibis' own formatter | **missing** — `compile_sql` is `ibis.to_sql` only | a "pretty SQL" toggle | S | cosmetic; `to_sql` already formats |

## Sampling

| capability | ibis surface | D8R status | where it would hook in | effort | recommendation |
| --- | --- | --- | --- | --- | --- |
| reservoir sampling | `Table.sample(fraction, method='row'|'block', seed=)` — no-seed form compiles on all D8R dialects except risingwave/druid; seeded form compiles only on duckdb, postgres, oracle, snowflake, materialize (verified; others raise `Table.sample with a random seed is unsupported`) | **missing** — `\sample` is not a command (verified parse error); the AI context's `sample_rows` tool is an unordered `LIMIT` (`d8r/ai/context.py:212`), i.e. a truncation, not a sample | `\sample 0.1 [seed 7]` command; payload `sample: {fraction, seed}`; `build`: `expr = expr.sample(...)` before limit; `supports.sampling` capability (duckdb/postgres yes; sqlite yes for fraction, no seed — verified) | M | recommended — and it should replace the AI tool's LIMIT-with-note honesty with a real sample |

## Catalog hierarchy (databases / schemas / catalogs)

Verified surfaces: duckdb `list_catalogs() → ['memory','system','temp']`,
`list_databases() → ['information_schema','main','pg_catalog']`,
`current_database`/`current_catalog` (and `attach`, `attach_sqlite`,
`read_sqlite`, `read_mysql`, `read_postgres`, `load_extension` — none used by
D8R). PostgreSQL exposes `list_catalogs`, `list_databases` (= schemas, which
is what `add_postgres_source` uses it for), `current_database`,
`list_tables`, `get_schema`, plus `create_database/drop_database`.

| capability | ibis surface | D8R status | where it would hook in | effort | recommendation |
| --- | --- | --- | --- | --- | --- |
| traverse databases/schemas | `con.list_databases()`/`list_catalogs()`/`con.table(name, database=(db, schema))` | **partial**: the postgres builder validates one chosen schema against `list_databases()` and discovers exactly that schema's tables (`datasources.py:718`); no traversal UI; duckdb demo is single-catalog by construction | the add-source modal's schema input → a picker fed by `list_databases()`; a `Schema`-pane tree per database | M | recommended for postgres sources — the modal already collects the schema name; a picker is strictly friendlier |
| cross-database queries | `con.table(name, database=…, catalog=…)` | **missing** (every dataset lives in the source's one schema) | `TableDef` gains an optional `database`; payload resolution passes it through | M | hold until asked |

## Dialect and backend coverage

Verified by compiling: D8R-shaped payloads (an aggregate+`\join`+`\group`+
`\order`+`\limit` query and a `\select … over (partition by … rows …)` +
`rank()` query) through `expression.compile_sql` for **all 20 `DIALECTS`
entries**: every `compiles: True` dialect renders both; **pyspark fails both**
with `AttributeError: 'NoneType' object has no attribute 'treat_nan_as_null'`
(needs live session config — `ibis.options.pyspark` exists but exposes no
usable fields in this build), so `compiles: False` is honest.

| finding | ibis surface (verified) | D8R status | where it would hook in | effort | recommendation |
| --- | --- | --- | --- | --- | --- |
| `singlestoredb` | an installed entry point **and** a compiler module, but `ibis.to_sql(t, dialect="singlestoredb")` raises `ValueError: Unknown dialect singlestoredb` in a bare process; it compiles only after `import ibis.backends.sql.compilers.singlestoredb` first (both verified) — an upstream export omission in 12.0.0 | **missing from `DIALECTS`** — which is *correct* for a build that reaches dialects only via `ibis.to_sql` | if added to `DIALECTS`, the app would need the import-order workaround — not worth it | S | leave out; footnote it in `DIALECTS` comment |
| `polars` | 22nd entry point, **not installed** (`ModuleNotFoundError: polars`), no compiler module, `to_sql` rejects it — it's a DataFrame passthrough backend, not a dialect | absent from `DIALECTS` — correct | — | — | no change |
| `pandas` backend | gone from 12 (no entry point, `to_sql` → `Unknown dialect pandas`) | not listed — correct | — | — | no change |
| "Flink-family" | only `flink` + `risingwave` exist, and **both are already in `DIALECTS`** (flink compiles D8R payloads, verified) | — | — | — | nothing to add |
| pyspark nuance | simple expressions compile, D8R-shaped payloads don't (verified both ways) | `compiles: False` | could refine the label to "compiles simple queries only" | S | keep `False` — the honest label for D8R's real payloads |
| feature-vs-dialect honesty | per-dialect failures measured for the feature lists above: `quantile` missing on sqlite/athena/mysql/impala (among others), `asof` not on sqlite/databricks, `unnest` not on mysql/flink, `collect` not on druid, `regex` not on mssql, `ntile`/`percent_rank` not on materialize/clickhouse, `sample` rule absent only on risingwave/druid | `CAPABILITIES` is per-source-**kind**, and D8R's live kinds are duckdb/sqlite/postgres only | only matters if D8R ever *executes* against a chosen compile dialect (it doesn't — compile ≠ run is by design) | — | no action; the table above is the reference if that ever changes |

## Misc

| capability | ibis surface | D8R status | where it would hook in | effort | recommendation |
| --- | --- | --- | --- | --- | --- |
| cached intermediate results | `Table.cache()` ("memoize" in old ibis; `ibis.memoize` **does not exist in 12**) — executed fine on the live connection in a probe, but its cleanup inherits the same broken `DROP` rendering documented for `drop_table` in AGENTS.md/`tx.py:13` | **missing** (equivalent: `\temp`, which is a *user-visible* temp table — strictly better for the document model) | — | — | not recommended; `\temp` is the D8R spelling |
| graphviz lineage | `expr.visualize()` → `ModuleNotFoundError: No module named 'graphviz'` in this build; `ibis.options.graphviz_repr` | **missing** (module not installed) | would need a dependency + a file-render path outside a terminal | L | skip |
| interactive repr | `ibis.options.interactive`, `ibis.options.repr.*` (rich terminal previews) | **missing** — the TUI renders its own table; D8R already formats rows itself | — | — | no value; D8R *is* the repr |
| in-memory views | `Table.view(name)` (expression-level naming, no DDL) | equivalent via `\with` CTEs | — | — | not a gap |
| selectors | `ibis.selectors`: `of_type`, `numeric()`, `starts_with`, `across(selector, fn)`, `any_of/any_` (verified compiling `s.of_type(…), s.across(…)`) | **missing** — `\select *` is a fixed star; column picking is by name | a `\select numeric_only()` sugar or palette filter — but honest value is low with the name-first palette | M | skip unless the document model grows a column-set concept |
| inline literal rows | `ibis.memtable({"col": […]})` (verified; renders a real inline relation on duckdb) | **missing** — the language has no literal table; `\where … in (…)` bare value lists are refused by grammar (a documented decision, `docs/AST.md:409`) | if bare `in`-lists are ever wanted: `isin(list)` compiles directly (`IN ('a','b')`, verified) without memtable | S | revisit the `in`-list decision with `.isin` — one engine branch |
| `Table.pipe`, `Table.set operations on multiple rest tables`, `ibis.set_backend`/`get_backend`/`ibis.connect` | various | **missing** — D8R builds backends explicitly per source kind; `pipe` has no textual-document analogue | — | — | not gaps |
| `Table.sql(query)` / `con.sql` / `ibis.parse_sql` | see raw SQL section | deliberate boundary | — | — | — |

## Deliberate boundaries

These ibis surfaces exist and work, and D8R **should not** expose them — each
is walled off by an AGENTS.md hard boundary ("Hand-written SQL generation —
Compose ibis expressions; render with `ibis.to_sql`"; "Persistence — … runtime
database/results state stays in memory"; the PostgreSQL exception's explicit
"…this authorizes no other database/network backend, server, login system or
arbitrary SQL execution"; "An HTTP server or a browser UI — The TUI is the
app"). Listed for completeness, with no recommendation:

| ibis surface | boundary it hits | note |
| --- | --- | --- |
| `con.sql(query)` / `Table.sql(query)` / `ibis.parse_sql` (SQL → expression or expression → *executed* SQL string) | no hand-written SQL generation; no arbitrary SQL execution | `ibis.to_sql` (expression → *display* SQL) is the sanctioned half, and D8R uses exactly it |
| `con.raw_sql`, `duckdb.settings`, `load_extension`, `attach`/`attach_sqlite`, `read_mysql`/`read_postgres`/`read_sqlite`/`read_xlsx`/`read_delta`/`read_geo`, `register_filesystem` | no new database/network backends; parquet-only ingestion by design (`_ingest` globs `*.parquet`) | `tx.py` uses raw statements only for the sanctioned temp/transaction DDL, per its own comment |
| `con.create_table`/`create_view`/`insert`/`upsert`/`truncate_table`/`rename_table`, `Table.to_delta`/`to_polars`/`to_torch`, persistent writes | persistence: only `\temp` writes, in-memory only | `\temp`+`tx.py` is the entire sanctioned write surface; `create_table` is additionally broken in this build's sqlglot pin |
| `ibis.connect` to any backend, e.g. `ibis.duckdb.connect(…)` to new files/DBs, polars/pandas backends | "no real database connections" + the two narrow user-authorized exceptions (D1, PostgreSQL) | D8R's registry (`datasources.py`) is the only door, and it is deliberate |
| `expr.visualize()` graph output, HTTP-ish `con` session features | "the TUI is the app" | `visualize` is also dependency-absent |
| `\fn` bodies becoming Python (ibis Python UDFs) | no arbitrary code; the document is text, `\fn` substitution is textual "on purpose" (`parser.py:482`) | listed honestly as *not stated* as a boundary verbatim — the Python-UDF row above treats it as a judgment call, this row treats it as the spirit of the wall |

One nuance worth keeping on the right side of the wall: the **`Export
results` gap** (Parquet/XLSX via `Table.to_parquet`/`to_xlsx`) is *file output
the user asks for*, in the same category as the existing CSV export — it is
recommended above despite the persistence boundary, whose scope is runtime
*database* state. If the maintainer reads the boundary stricter than that,
treat that row as a boundary item instead.
