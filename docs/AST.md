# anyQL AST contract

The document is a sequence of `\command` lines. `parse_query(doc)` — in
**`anyql/query/parser.py`** — is a **pure function of the document text**: the
AST is always recomputed from the text, so nothing about the editor (undo/redo,
caret moves, palette inserts) can leave the AST and the document out of step.
The AST types and their `to_json()` wire shape live in **`anyql/query/ast.py`**;
the derived output-name rules in **`anyql/query/alias.py`**; the schema-registry
seam the parser validates against in **`anyql/query/schema.py`**. The TUI
(`anyql/tui/`) is a consumer of all of it — `Session.run` calls `parse_query`
then `payload_from_ast`, and `anyql/tui/palette.py` reads the same AST to decide
what `\` offers.

The canonical contract is the pair:

- `spec/canonical-query.anyql` — the exact command text
- `spec/canonical-query.ast.json` — the exact expected AST for that text

Acceptance is:

```bash
uv run python verify/check_canonical.py   # deep equality of parse_query(doc).to_json()
                                          # against the fixture — prints MATCH, exits 0
uv run pytest -q                          # the whole suite, which pins the same
                                          # contract in tests/test_query.py
```

Parser-semantics changes update the fixture, `tests/test_query.py`, and this
document together.

## Commands

| command | argument | effect |
| --- | --- | --- |
| `\with` | `<name>` + indented body | defines a reusable sub-query (CTE), repeatable |
| `\from` | `<dataset> \| ( <subquery> )` `[as] alias` | sets `from` (a later `\from`/`\open` replaces it) |
| `\open` | same as `\from` | identical to `\from` — the dataset-source spelling |
| `\join` | `<dataset> \| ( <subquery> ) [as] alias on col[ = col]` | appends to `joins` (repeatable; always INNER) |
| `\join lateral` | `( <subquery> ) [as] alias [on col[ = col]]` | appends a lateral join / `CROSS APPLY` (repeatable) |
| `\union` | `[all\|distinct] <dataset\|cte\|( subquery )>` | appends to `setOps` (repeatable; distinct unless `all`) |
| `\intersect` | `[all\|distinct] <dataset\|cte\|( subquery )>` | appends to `setOps` (repeatable; keeps shared rows) |
| `\except` | `[all\|distinct] <dataset\|cte\|( subquery )>` | appends to `setOps` (repeatable; subtracts the operand) |
| `\select` | expression | appends to `select` |
| `\distinct` | — | removes duplicate output rows after projection and set operations |
| `\unique` | — | alias for `\distinct` |
| `\where` | `column op value`, or `[not] in ( subquery )` | sets `where` (a later `\where` replaces) |
| `\group` | `column[ column…]` | appends to `groupBy` |
| `\order` | `target [asc\|desc]` | appends to `orderBy` (default `asc`) |
| `\case` | `<alias> = when … then … [else …]` | appends a computed column (repeatable) |
| `\limit` | non-negative integer | sets `limit` (a later `\limit` wins) |
| `\temp` | `<name>` | materializes this document's query as a temp table (a later one replaces) |
| `\drop` | `<name>` | drops a temp table this session created (a later one replaces) |
| `\begin` | — | opens a transaction on the active source |
| `\savepoint` | `<name>` | marks a savepoint in the open transaction |
| `\release` | `<name>` | releases a savepoint, keeping its work |
| `\rollback` | `[to <name>]` | rolls the transaction back, whole or to a savepoint |
| `\commit` | — | commits the open transaction |

Commands are case-insensitive (`\FROM` is `\from`); the command name is
lowercased into the AST. A line that does not start with `\` is the error
`` not a command — lines start with \ ``; an unknown name is
`unknown command "\<name>"`; a command that needs arguments and got none is
`\<cmd> expects arguments` (`from`, `open`, `join`, `union`, `intersect`,
`except`, `select`, `where`, `group`, `order`, `limit`, `case`, `with`, `temp`,
`drop`, `savepoint`, `release`).

Argument rules:

- No commas are needed in lists: `\group customer_id region` is two group terms
  (comma-separated input works too, as does any mix: `\group user_id, region
  status`).
- Anything with a space is a wrapped string: `\where name = John Smith` stores
  the value `John Smith` without requiring quotes. Quotes are optional and one
  matching layer is stripped when present (`"paid"` and `'paid'` both store
  `paid`); a value containing a quote character keeps it.
- Expressions in `\select` are one of: a plain column (`customer_id`), a
  **literal constant** (`'x'`, `"1"`, `1`, `-2.5`, `true`, `null`), a
  **star** (`*`, see below), an aggregate call over a column (`sum(amount)`) or
  a row count (`count(*)`),
  a **temporal extraction** over a temporal column (`year(timestamp)`,
  `hour(e.occurred_at)`), a **rank call** (`rank()`, `dense_rank()`,
  `row_number()` — no argument), a **regex call** (`regexp_extract(path,
  "/p/([0-9]+)")`, `regexp_replace(path, "/p/[0-9]+", "/page")`), a
  **catalog scalar call** (`concat(upper(path), '-', string(user_id))`, see
  below), or an inline **subquery** (`( … ) as peak`). Any aggregate or rank
  call may carry a trailing **`over ( … )` window frame** (see below). Each
  item optionally ends with `as alias`, case-insensitively. Comma-separated expressions on one line
  are separate select items (`\select user_id, sum(amount) as total` yields
  two), split only at paren depth 0 — and a comma inside a quoted pattern or a
  subquery stays part of it.
- `\join users on user_id` joins dataset `users` on the shared column
  `user_id` (left side = right side). `\join users on user_id = id` names both
  sides, either may be identifier-qualified. Joins apply in document order, each
  onto the result of everything before it, always as an INNER join. A malformed
  clause (missing `on`, or a shape the grammar does not read) is
  ``\join expects `<dataset> [as] <alias> on <col>[ = <col>]` ``.
- A bare second identifier after the dataset is a **table alias**
  (`\from events e`, `\join users as u on user_id`); the `as` keyword is
  optional. Duplicate identifiers among open tables are the error
  `duplicate table identifier "x"`.
- Type compatibility is **not** the parser's business: a `year` on a float
  column parses, and the engine refuses it at build time
  (`unknown function: 'year' for float64`). Completion keeps it from happening
  interactively by filtering on the capability map.

### Literal constants

`\select` accepts single- or double-quoted strings, signed integers, decimal
and scientific-notation numbers, and case-insensitive `true`, `false`, `null`.
Quoted numbers remain text: `'1'` is a string, not the integer `1`. Double the
delimiter inside a string to include it (`'it''s'` → `it's`); commas and clause
keywords inside quotes remain part of that value. Backslashes are literal,
not escape sequences.

```anyql
\from events
\select user_id, 'x' as marker, '1' as text_one, 1 as number_one, null as missing
\limit 2
```

The AST and payload carry `literal: {"value": <typed scalar>}` on these select
items; `literal: null` means the item is not a literal. The wrapper preserves
the distinction between an explicit NULL and an absent expression. An explicit
`as` alias names the output; without one, the expression's original text is
its name (`'x'`, `1`, etc.). Aliases work as `\order` targets normally.

Constants project once per source row, including zero rows for an empty input.
Alongside aggregates they are added to the aggregated result, never introduced
as grouping keys: a global count over an empty input still returns one row.
A literal cannot carry `over (...)`. A source is still required; this does not
introduce source-free queries or general arithmetic expressions.

FN bodies use the same rule: `\select @tag as tag, 'x' as marker` accepts a
quoted text or numeric argument after substitution, with its type preserved.

### Scalar string functions

Scalar calls accept bare or qualified columns, the typed literals above, and
other catalog scalar calls. Names are case-insensitive and stored lowercase.
For example:

```anyql
\from events e
\select concat(upper(e.path), '-', string(e.user_id)) as label
\select concat_ws(', ', e.path, 'it''s, literal text') as description
\select substr(e.path, 0, 3) as prefix
\limit 10
```

The complete scalar catalog is below. `text`, `separator`, `prefix`, `suffix`,
`old`, `new`, `needle`, and `pad` require string values; `start`, `length`, and
`count` require integers. Each argument can itself be a compatible column,
typed literal, or scalar call. Brackets denote optional positional arguments;
`...` permits more string arguments, never empty argument slots.

| signature | result / behavior |
| --- | --- |
| `string(value)` | string; explicit Ibis cast from any value |
| `concat(text, text, ...)` | string; join at least two strings without a separator |
| `concat_ws(separator, text, ...)` | string; join one or more strings with a separator |
| `lower(text)` | string; lowercase |
| `upper(text)` | string; uppercase |
| `capitalize(text)` | string; capitalize |
| `length(text)` | integer; string length |
| `strip(text)` | string; trim whitespace from both ends |
| `lstrip(text)` | string; trim leading whitespace |
| `rstrip(text)` | string; trim trailing whitespace |
| `substr(text, start[, length])` | string; substring starting at a **0-based** offset |
| `left(text, count)` | string; leftmost characters |
| `right(text, count)` | string; rightmost characters |
| `replace(text, old, new)` | string; literal text replacement |
| `contains(text, needle)` | boolean; literal substring membership |
| `startswith(text, prefix)` | boolean; prefix membership |
| `endswith(text, suffix)` | boolean; suffix membership |
| `repeat(text, count)` | string; repeat the input |
| `reverse(text)` | string; reverse characters |
| `lpad(text, length[, pad])` | string; left-pad (default padding is a space) |
| `rpad(text, length[, pad])` | string; right-pad (default padding is a space) |
| `find(text, needle[, start])` | integer; **0-based** position, `-1` when absent |

Positions follow **Ibis**, not a backend SQL dialect's 1-based positions.
`find`'s optional `start` is also 0-based; `substr` without a length extends to
the end. anyQL builds Ibis expressions rather than writing SQL. Backend support
and string/cast behavior (including Unicode, padding, and unusual offsets) can
differ; unsupported operations surface as build/compile/execution errors.
Live D1 does not offer `capitalize`, `reverse`, `repeat`, `lpad`, or `rpad`:
the installed Ibis SQLite compiler implements them with Python UDFs available
in a local SQLite snapshot, not over D1's HTTP connection.
Arguments are not implicitly converted to text: use `concat(path,
string(user_id))`, not `concat(path, user_id)` for a numeric ID. Type checks
belong to the engine; parsing validates grammar and arity, not column dtypes.
NULL remains a typed literal node, is passed to Ibis, and follows the operation's
Ibis/backend semantics; anyQL does not replace NULLs or impose its own concat
NULL behavior.

Unknown function names, wrong argument counts, and empty arguments such as
`concat(path,, 'x')` are errors on settled lines. Scalar arguments cannot contain
aggregates, temporal/regex/rank calls, subqueries, windows, or arbitrary method
invocations. A scalar call itself cannot carry `over (...)`. Existing dedicated
aggregate, temporal, regex, and rank syntax remains unchanged.

An explicit alias wins; without one the original expression text is the output
name (for example, `upper(path)`). Column-dependent calls project per source row
and become implicit grouping keys alongside aggregates, like temporal calls.
Constant-only calls use the literal-constant path beside aggregates: they are
not dropped and are not grouping keys.

Only `\select` gains expression syntax. `\where` and `\group` still take column
names; compute a value in a CTE to filter or group it:

```anyql
\with labeled
  \from events
  \select user_id, lower(path) as normalized_path
\from labeled
\where normalized_path = '/checkout'
\select user_id, normalized_path
\limit 10
```

The AST and payload always include `scalar` on select items (`null` for all
other expression kinds). A call is `{"fn": "concat", "args": [...]}`; each
argument is exactly `{"column": "e.path"}`, `{"literal": {"value": "-"}}`,
or another `{"fn": ..., "args": [...]}` call. This wrapper preserves explicit
NULL as `{"literal": {"value": null}}`. The payload always supplies the effective
output alias; a direct engine payload without an alias defaults to the function
name.

### Star

- **Star** (`\select *`) expands to every column of the open tables, leftmost
  table first with duplicate names skipped — exactly the empty-select
  projection (see below). It is a whole-table projection only: `*` cannot take
  an alias (`` `*` cannot take an alias ``), cannot carry a frame
  (`` `*` cannot carry over (...) ``), cannot mix with an aggregate or `\group`
  (`star select cannot mix with aggregates or group by`), and a qualified `t.*`
  is not supported (it fails to parse as an expression). Mixed on one line it
  keeps its position and dedups against the other items already projected
  (`\select user_id, *` projects `user_id` first, then the remaining columns).

`count(*)` is a row-count aggregate, not a star projection. It counts every row
of the current joined and filtered relation, including rows with NULL values;
`count(column)` counts only non-NULL values in that column. Both support grouping
and window frames. A global count on empty input returns one row containing zero.
Other aggregates cannot take `*` and are rejected by the engine. The AST keeps
`count(*)` as `aggregate: {"fn": "count", "arg": "*"}`. Its existing derived
alias is `__count`; an explicit `as` alias overrides it, including for windows.

### Window frames

- `<expr> over ( [partition by <col>…] [order by <col> [asc|desc]] )` attaches a
  window frame to a select item. Inside the parentheses at least one of
  `partition by` / `order by` must be present; anything else is
  `over (...) needs partition by <cols> and/or order by <col> [asc|desc]`.
  Parsing is case-insensitive and tolerant of spacing (`OVER (PARTITION BY …)`).
- A windowed aggregate is a per-row partition total
  (`sum(amount) over (partition by customer_id)`); a rank call **requires** the
  frame and its `order by` (`rank() over (order by placed_at desc)`), and it
  renders 1-based on both sides. Errors: `<fn>() requires over ( ... )`,
  `<fn>() requires order by inside over (...)`, `"year" is not a window
  function — use sum/avg/count/min/max` (temporals cannot be windowed), `a
  plain column cannot carry over (...) — wrap it in a function`.
- Windowed items cannot mix with plain aggregates or `\group`
  (`windowed select items cannot mix with aggregates or group by`), and
  case/window columns cannot mix with aggregates
  (`case/window columns cannot mix with aggregates`) — both engine errors.
- Windowed aggregates take the same `<arg>_<fn>` derived alias rule; ranks
  default to the rank name (`rank`, `dense_rank`, `row_number`).

### `\case`

- `\case <alias> = when <col> <op> <val> then <val> [when … then …] [else <val>]`
  adds a computed output column (repeatable). The **alias is required** (there is
  no derived auto-alias — the clause name *is* the output name); a malformed
  clause is `` \case expects `<alias> = when <col> <op> <value> then <value>
  [when …] [else <value>]` `` and is not added to `cases`.
- Operators are the same set `\where` accepts (`=`, `!=`, `>`, `>=`, `<`,
  `<=`, `like`); `then`/`else` values follow the same quote-stripping rule as
  `\where` values. A quoted value containing `when`/`then`/`else` never splits a
  branch — keyword scanning skips quoted strings and parentheses.
- Case columns are projected after the select list. `\else` is optional;
  the AST stores `else: null` and the engine maps it to SQL `ELSE NULL`.

### `\with` (CTEs)

- `\with <name>` starts a **CTE block**: the lines indented under the header
  form a complete sub-query (`\from`, `\select`, … with the same grammar). A
  blank line stays part of the block only when the next non-blank line is
  indented; a flush line ends it. The body must be non-empty
  (`\with a expects an indented body`), and the name must be a bare identifier
  (`\with expects a bare CTE name`).
- A CTE is addressable by name from a later `\from`/`\join` (and as a
  `\union`/`\intersect`/`\except` operand), in document order (a *forward*
  reference is `unknown table "x"`); nested CTEs (a `\with` inside a
  body) are the error `nested CTEs are not supported`; a repeated name is
  `duplicate CTE name "a"`; a name colliding with a *loaded* dataset is
  `CTE name "events" shadows dataset "events"` (the engine's build order would
  make the dataset unreachable).
- A CTE body validates like the main query — same identifier/column rules,
  scoped to its own open tables, and it sees every CTE defined **before** it.
  Body errors bubble to the document with their absolute line numbers;
  `with_[i].body.errors` is always empty.

### `\distinct` / `\unique`

Both zero-argument commands set the current query's `distinct` boolean to
`true`; repeating either spelling is idempotent. They deduplicate **complete
output rows**, not source rows or each selected column separately. With no
`\select`, every output column participates; computed columns, constants, and
aliases participate just like plain columns. Duplicate NULL rows collapse,
and empty input stays empty.

```anyql
\from events
\select event_type
\distinct
\order event_type
\limit 3
```

The bundled events have four distinct event types; this example returns the
first three in sorted order. Replacing `\distinct` with `\unique` is identical.
Execution within each query block is source/joins → filter → projection and
aggregation/computed columns → set operations → distinct → order → limit,
regardless of where its command is written. CTEs, inline subqueries, and FN
bodies each have their own independent flag. Set-operation `all`/`distinct`
modifiers remain independent of this row-level command.

Arguments such as `\distinct event_type` are errors on settled lines. This
does not add a SELECT modifier, `distinct(column)`, or aggregate DISTINCT
syntax. AST JSON and generated payloads always include `distinct: false`
unless requested; direct payloads may omit it (false), but a supplied value
must be a boolean, never `null`, `0`, or a string.

### Set operations

- `\union` / `\intersect` / `\except` append to `setOps` in document order,
  exactly as `\join` appends to `joins`: the operations run left-deep over the
  accumulated left query, so `\union a` then `\except b` is `(left ∪ a) \ b`.
  `\except` is SQL's `EXCEPT` — it subtracts the operand's rows.
- The modifier is SQL's and precedes the name: `\union [all|distinct] <name>`.
  Absent means `distinct`, so `\union` deduplicates and `\union all` keeps
  duplicates; all three take both spellings (`\intersect all`, `\except
  distinct`). A bare modifier names nothing and is
  `` \union expects `[all|distinct] <dataset>` ``.
- The operand is a **table name** — a dataset of the active source or a CTE
  defined **before** the clause (the same rule as `\from`/`\join`, including
  the forward-reference error `unknown table "x"`). Any table a `\from` accepts
  is a legal operand.
- The **left query's output columns define the operation**: the operand is
  projected to exactly those columns, in that order, so extra operand columns
  are dropped and one the operand lacks is the engine's
  `` \union "x" is missing <col> — it projects: … ``. A column present on both
  sides with different types is refused the same way, as a `PayloadError`.
- Set operations apply after the projection (`\select`, `\case`, window items)
  and **before** row-level `\distinct`, then `\order`/`\limit`: ordering and
  limiting apply to the merged, optionally deduplicated result. A block is
  still one query — an operand is a name or an inline `( … )` subquery, and
  anything longer-lived goes in a `\with` body.

### Subqueries

An **inline subquery** is a parenthesized sequence of commands, written on the
clause's own line: `( \from … \select … \where … )`. It is the same grammar,
the same parser, and the same validator as the document — a subquery *is* a
query, not a second language — and it nests (a body may hold subqueries of its
own). Every node inside it reports the document line the clause sits on, so a
mistake in a body points at real text, and a body's errors bubble up to the
document; `errors` on the nested AST is always `[]`.

```anyql
\from (\from events \select user_id, sum(amount) as total \group user_id) as totals
\select totals.user_id
\select totals.total
\where user_id in (\from users \select user_id \where region = "emea")
\select (\from events \select max(amount)) as peak
```

Three positions accept one:

- **Table position** — `\from`/`\open`, `\join`, and the three set operations.
  The dataset name becomes the empty string (`table`/`dataset`) and `body`
  carries the sub-query. Such a source **needs an alias** in `\from`/`\join`
  (`a subquery source needs an alias — write ( … ) as <name>`): the alias is the
  identifier later clauses address it by, exactly as for a dataset. A set-op
  operand takes no alias (nothing addresses it). The body is built as a relation
  and joined — never inlined SQL.
- **`\where … in ( … )` / `not in ( … )`** — a semi/anti join over the
  subquery's single column; a subquery that projects more (or fewer) is the
  engine's `` `in` needs a subquery of exactly one column — it projects: … ``.
  `in` without a subquery is `` `in` expects an inline subquery — write
  ( \from … ) ``: this language has no bare value list.
- **`\where <column> <op> ( … )`** and **`\select ( … ) as <alias>`** — a
  **scalar subquery**, compared against a column or projected as one. Both need
  exactly one column (`` a scalar subquery must project exactly one column — it
  projects: … ``), and the `\select` form needs an alias (`a scalar subquery
  needs an alias — write ( … ) as <name>`) since there is nothing to derive a
  name from. A scalar subquery is a computed column like a `\case` or a window
  item: it cannot mix with aggregates or `\group` (`a scalar subquery cannot mix
  with aggregates or group by`).

A parenthesized operand is read as a subquery when it **contains a command**:
`\where amount = (3)` keeps comparing against the value `(3)`, so the literal
never turns into an empty body — and an operator that takes text (`like`, `~`)
keeps its operand as written, subquery-shaped or not. Correlation is not a
subquery feature here — only a lateral body may read outside itself (next
section).

### LATERAL joins / CROSS APPLY

`\join lateral ( … ) as <alias> [on <col>[ = <col>]]` joins a subquery that may
read the tables to its **left** — SQL's `JOIN LATERAL`, and `CROSS APPLY` when
the `on` is left off. The body correlates through its `\where`, as one equality
against an outer column (qualified — `\where e.user_id = u.user_id`); anything
else is refused
(`` a lateral body correlates through `= <outer column>` in its \where: … ``).
The left side is visible to the body's *own* clauses; a subquery nested inside
the body reads nothing from outside it, because the correlation is the body's
`\where` and nothing deeper — there, an outer-looking value is an ordinary value
again.

```anyql
\from users u
\join lateral (\from events e \where e.user_id = u.user_id \order timestamp desc \limit 2) as recent
\select u.user_id, u.region, recent.timestamp
\order u.user_id
```

Two things make it *lateral* rather than a decorated `\join`:

- the correlated equality is **hoisted** out of the body and into the join's own
  predicate (`ON events.user_id = users.user_id`), which is the same relation
  SQL's LATERAL produces — ibis has no correlated subquery inside a join, and
  the engine's job is to express the same rows, not to write SQL by hand;
- a `\limit` in the body means **per left row**: the engine numbers the matched
  rows inside the correlation's partition, in the body's own `\order`, and keeps
  the first `\limit` of each — the top-N-per-group rewrite every planner
  performs. A body that caps rows without an `\order` is
  `a lateral body that caps rows needs \order — the cap is per left row`.

An uncorrelated `( … )` body keeps its own global `\limit` and `\order` (a
`CROSS APPLY` of a plain subquery is a cross join). The body is one relation, so
everything it does not correlate over has to be written inside it — a local
filter rides in a `\with` body the lateral body then names. The correlation
column has to survive the body's `\select` (`unknown lateral key: …` otherwise),
the join's `on` accepts either side qualified, which is what a derived table's
column needs, and a table inside the body may not reuse a name from its left
(`duplicate table identifier "u" — a lateral body cannot reuse a name from its
left`): every reference to that name would be ambiguous.

### Table-valued functions

A **table-valued function** is a named query the session holds (the `\fn` page,
`anyql/tui/fn.py`) with a positional signature. Its **body** is an ordinary
anyQL document written with `@name` parameter slots, and a call fills each slot
with its argument:

```anyql
\from hot(1000) as h          # or \open, \join, \join lateral, or a set-op operand
\select h.user_id, h.amount
```

- A call is **`name(arg, …)`** at any table position `\from`/`\open`/`\join`/a set
  operation already accept. The arguments are **positional only** — bound to the
  signature in order — and each is a value or an inline `( \from … )` subquery,
  never a command (text with a `\` is refused, not spliced in). A repeated
  clause addresses the call's columns through its **alias, which defaults to the
  function name**, exactly as a dataset's would (`hot.user_id`, or `h.user_id`
  under `as h`).
- Expansion is **textual, at parse time** (`substitute_params`): every `@name` in
  the body is replaced by its argument, and the result is parsed as the relation
  at that site — the same machinery as an inline subquery, on the call's line. So
  the engine sees **literals** and **ibis never learns a function existed**: a
  call composes wherever a sub-query does (CTE body, nested subquery, set-op
  operand), and a plain `( … )` table-position limitation applies to it unchanged.
- It is **not** a SQL `CREATE FUNCTION` and survives no round-trip as a named
  callable: `\fn` is a TUI-layer construct. The function is `FnDef(name, params,
  body, doc)` in the session's registry; `set_fns` seams it to the parser on every
  `refresh_schema`, and `parse_body` validates a body on save against the live
  schema so it can only fail where it is written.
- Errors name the mistake at the call site: `unknown function "x" — defined: …`,
  `x() takes N argument(s) — got M`/`got none`, `a function call needs a closing
  ")" — x(...)`, and `an argument to x() cannot contain \ …`. With an **empty**
  registry (the pure-parse regime the canonical fixture runs in) a call is not a
  function at all — the site falls through to its ordinary dataset-name error.
- The function body editor offers `@name` from its current parameter declaration
  field, including unsaved edits. Bare `@` lists valid names once each; a partial
  name filters them. Completion uses the parser's parameter-token quote and
  word-boundary rules, replaces the whole token when editing its middle, and
  preserves surrounding calls. These suggestions are local to the form, not the
  workspace registry, and obey the IntelliSense switch and Escape dismissal.

### Window frames

`over ( … )` may bound its frame as well as partition and order it:

```
over ( [partition by <col>…] order by <col> [asc|desc]
       [rows|range between <start> and <end>] )
```

Bounds are `<n> preceding`, `current row`, `<n> following` — either end also
`unbounded preceding` / `unbounded following` — and a frame requires the
`order by`, because that is what it is counted from
(`over (...) frame needs order by <col> [asc|desc] and bounds of …`). `rows`
counts rows, `range` counts ordering values:

```anyql
\select sum(amount) over (partition by user_id order by timestamp rows between unbounded preceding and current row) as running_total
\select sum(amount) over (order by timestamp range between 2 preceding and current row) as window_total
```

Frames ride on windowed items exactly like the rest of the frame clause: they
cannot mix with plain aggregates or `\group`, and a rank call may carry one.

### Regular expressions

Regex is a predicate operator and a pair of projections, all on string columns:

- `\where <column> ~ <pattern>` matches (POSIX, unanchored — a partial match is
  a match), `!~` negates it. Patterns are wrapped strings like any value
  (`~ "pur|cli"`), and the same two operators work in `\case` branches. A
  non-string column is `` `~` needs a string column: 'user_id' is int64 ``.
- `regexp_extract(<column>, <pattern>[, <group>])` returns the match — the whole
  match, or the named capture group when one is given (`regexp_extract(path,
  "/p/([0-9]+)", 1)`); `regexp_replace(<column>, <pattern>, <replacement>)`
  replaces every match. Both are row-wise string columns, so they group like any
  plain column, take the derived `<arg>_<fn>` alias when unaliased
  (`path_regexp_extract`), and cannot carry `over ( … )` (`"regexp_extract" is
  not a window function — use sum/avg/count/min/max`).

`~`/`!~` reach the engine as `REGEXP_MATCHES`, the extraction/replacement as
`REGEXP_EXTRACT`/`REGEXP_REPLACE`; a malformed pattern is the engine's own error
at run time, not a parse error.

### Temp tables

`\temp <name>` materializes the document's query as a **temp table** on the
active source: the run answers with the rows it asked for *and* keeps them, and
the table joins the source's datasets — the schema tree, the parser's registry,
and the palette's offers all see it, so a later document can say `\from <name>`
or join it. `\drop <name>` removes one. Both are in-memory only: the table lives
on the connection and dies with the process (AGENTS.md's rule), and a name
already taken by a dataset is
`"<name>" is already a dataset — pick another temp name`; `\drop` refuses
anything that is not a temp table this session created
(`"<name>" is not a temp table`).

```anyql
\from events
\select event_type, sum(amount) as total
\group event_type
\temp event_totals
```

Re-running the document rewrites the table (`CREATE OR REPLACE TEMPORARY
TABLE`), which is what an IDE's run key should do. `\temp` needs a query
(`\temp needs a query to keep — this document has no \from`), and a live D1
source cannot hold one at all (`a live D1 source cannot hold a temp table — it
is reached over HTTP`), since nothing about it is local. A snapshot can.

### Transactions and savepoints

The transaction commands are **statements**, not clauses: they run against the
active source's own connection, in the order they were written, and the
document's query then runs last inside whatever state they left — which is how a
run in this IDE wraps one query in the session's transaction. A document of
statements alone carries no query and answers with their status.

```anyql
\begin
```
```
\from orders
\temp paid_orders
```
```
\savepoint before_cleanup
```
```
\rollback to before_cleanup
```
```
\commit
```

- `\begin` opens one; a second is
  `a transaction is already open on demo — \commit or \rollback first`.
- `\commit` / `\rollback` end it (`no transaction is open on demo — \begin
  first` when there is none). `\rollback to <name>` rewinds to a savepoint and
  keeps it; `\release <name>` drops it, and every savepoint after it. An unknown
  name lists what is open:
  `unknown savepoint "s1" — open: before_cleanup`.
- State is **per source** and lives in the session (never on disk); switching
  sources does not carry a transaction across.
- What an engine has is what it is told: DuckDB keeps whole transactions but has
  no savepoints (`the duckdb engine has no savepoints — it keeps whole
  transactions (\begin, \commit, \rollback)`), SQLite — a D1 snapshot — has
  both, and a live D1 has neither (`a live D1 source has no transactions — it is
  reached over HTTP, so every query stands alone`). The capability flags behind
  that are in `datasources.capabilities_for`, and the palette reads them:
  `\savepoint` and `\release` are not offered where the engine has none.
- `\temp` joins the transaction: a table created inside one is dropped again by
  a `\rollback` (or by `\rollback to` a savepoint older than it), and a `\drop`
  inside one is undone by it — the table comes back, and the explorer with it.
  Temp-table DDL is transactional on both engines, which is the observable
  effect a transaction has here. A
  *failed statement* does not abort the transaction (DuckDB's own behaviour), so
  a session that wants a clean slate says `\rollback`.

## Errors, and the line being typed

- `errors` only reports **settled** lines. The last non-empty line of the
  document is the line being typed, where half-finished commands are normal:
  `\from nope`, `\where amount =`, `\select sum(` and `\case f = when ` all
  report no error there. Trailing blank lines do not move the typing line; a
  settled line before it still reports.
- Validation splits by registry state. `unknown table "nope" — loaded
  datasets: events, users`, `unknown column "events.user_id" — "events" is not
  an open table: e` (qualified prefixes), and the CTE-shadows-dataset check fire
  **only once the schema registry is non-empty** — the parser stays quiet while
  it legitimately knows nothing. Duplicate table identifiers, duplicate CTE
  names, nested CTEs, and all grammar errors fire regardless of the registry.
- A qualified reference is checked in `\select` (including aggregate/temporal
  arguments, recursively nested scalar arguments, and window `partition by`/`order by`), `\where`, `\group`,
  `\order`, `\case` conditions, and **both** operands of a `\join`'s `on` — a
  qualified `on` side names whichever relation it points at, so its prefix must
  be an identifier open *at* that clause: everything before it **plus the alias
  the clause itself establishes** (`a.k = b.k` and `b.k = a.k` are the same
  equality, and a derived table's column is reached from either side). A *bare*
  `on` name keeps left/right scoping (left resolves to the accumulated left, the
  right to the clause's own table). With no `\from` at all the message lists `(no \from)`.
- An `\order` target that matches nothing is **not** an error: it parses with
  `resolvesTo: null` and the engine refuses it at run time
  (`unknown orderBy target: 'ghost'`).
- An inline subquery is validated like the document it is: its clauses report on
  the clause's own line, its errors bubble to the document, and its own
  `errors` list is always `[]`. Inside a body the strict regime needs the
  registry exactly as outside it — a body's qualified reference is checked
  against the body's open tables (plus, for a lateral body, the identifiers to
  its left).

## Shape

`QueryAST.to_json()` emits the documented wire shape — exactly the keys
`spec/canonical-query.ast.json` carries (Python fields are snake_case;
`with_`, `from_`, `group_by`, `order_by` map to `with`, `from`, `groupBy`,
`orderBy`):

```jsonc
{
  "with": [{ "line": 1, "name": "recent", "body": { /* full QueryAST; errors always [] */ } }],
  "from": {
    "line": 4, "table": "recent", "alias": null,   // table: "" for an inline source
    "body": null                                   // or a full QueryAST — the `( … )`
  },
  "joins": [{
    "line": 2, "dataset": "users", "alias": "u", "left": "user_id", "right": "id",
    "lateral": false,                              // true for `\join lateral`
    "body": null                                   // or a full QueryAST
  }],
  "setOps": [{
    "line": 10, "op": "union", "dataset": "archived", "distinct": true,  // op: union|intersect|except
    "body": null                                   // or a full QueryAST
  }],
  "select": [{
    "line": 7, "raw": "sum(amount) over (partition by customer_id) as customer_total",
    "column": null,          // plain column, else null
    "literal": null,         // { "value": "x" | 1 | 2.5 | true | null } for a constant
    "scalar": null,          // { "fn": "upper", "args": [{ "column": "path" }] } or null
    "star": false,           // \select *
    "aggregate": { "fn": "sum", "arg": "amount" },   // or null
    "temporal": null,        // { "fn": "month", "arg": "placed_at" } or null
    "rank": null,            // { "fn": "rank" } or null
    "window": {              // or null
      "partitionBy": ["customer_id"],
      "order": null,         // { "column": "placed_at", "direction": "desc" } or null
      "frame": null          // { "kind": "rows"|"range", "start": "…", "end": "…" } or null
    },
    "regex": null,           // { "fn": "regexp_extract", "arg": "sku", "pattern": "…",
                             //   "group": 1, "replacement": null } or null
    "subquery": null,        // a full QueryAST — a scalar `( … ) as x`
    "alias": "customer_total"   // explicit alias, else null
  }],
  "distinct": false,        // true for \distinct or \unique; always present
  "where": {                 // or null
    "line": 3, "raw": "status = \"paid\"", "column": "status", "op": "=", "value": "paid",
    "subquery": null         // a full QueryAST for `in`/`not in` and scalar comparisons
  },
  "groupBy": [{ "line": 5, "column": "customer_id" }],
  "orderBy": [{ "line": 10, "target": "customer_total", "direction": "desc", "resolvesTo": 3 }],
  "cases": [{
    "line": 9, "raw": "flag = when amount > 100 then \"high\" else \"low\"",
    "alias": "flag",
    "whens": [{ "column": "amount", "op": ">", "value": "100", "then": "high" }],
    "else": "low"            // or null
  }],
  "temp": { "line": 20, "name": "snapshot" },   // or null — `\temp`
  "drop": { "line": 21, "name": "previous" },   // or null — `\drop`
  "tx": [{ "line": 4, "kind": "begin", "name": null }],  // document order
                             // kind: begin|commit|rollback|rollback_to|savepoint|release
  "limit": 5,                // or null
  "errors": [{ "line": 2, "message": "…" }]
}
```

- `line` is a 1-based document line number; every node of an inline subquery
  reports the line its clause sits on, because that is the only line it has.
- `from.table` / `joins[i].dataset` / `setOps[i].dataset` are **empty strings**
  when that source is an inline `( … )` subquery, whose `body` then carries it.
- `direction` is `"asc"` or `"desc"`; the default is `asc`. `\limit` takes the
  last non-negative integer; anything else is
  `limit must be a non-negative integer` and leaves the previous value in place.
- `value`/`then`/`else` strings are stored with surrounding quotes stripped;
  `raw` keeps the line as typed (the `\case` raw excludes the command word). A
  `\where` whose operand is a subquery stores `value: ""`.
- `setOps[i].distinct` is SQL's default (`true`) unless the clause said `all`.
- Query-level `distinct` is `false` by default and independent of those
  set-operation modifiers; `\unique` normalizes to the same flag.
- `window.frame` bounds are canonical text (`unbounded preceding`,
  `2 preceding`, `current row`, `1 following`, `unbounded following`) whichever
  case and spacing they were typed with.
- `resolvesTo` is a 1-based index into `select` when an `\order` target matches
  a select alias or column, else `null`. It also covers derived aliases:
  `\order amount_sum` resolves to a `sum(amount)` select item. A `\case` output
  name resolves to `null` (a real output column, but not a select item) without
  an error. Windowed select items are ordinary select items — an `\order` at
  their alias resolves normally.

## Column resolution, identifiers, and auto-aliasing

Each open table (the source `\from`/`\open`, then every `\join`ed dataset in
document order) has an **identifier**: its alias if given, otherwise its
dataset name. References in `\select` / `\where` / `\group` / `\order` (and
aggregate/scalar arguments, window frames, and `\case` conditions) are either:

- **qualified** — `u.region` / `users.region`: one dot, the prefix must match
  an open table's identifier; it resolves to exactly that table. Aliasing is
  strict: once `\from events e` is set, `events.user_id` is an unknown-column
  error — only `e.user_id` resolves.
- **bare** — `region`: resolves to the **leftmost open table** that has the
  column (aliased or not — bare names match the underlying dataset's columns).

An empty select list (no `\select` at all) projects every column of the
source, then each joined table's columns **except names already projected** —
so a shared join key appears once, from the left. `\select *` produces the
same projection explicitly.

An **aggregate or temporal extraction with no explicit alias** gets a derived
output name `<arg>_<fn>` (both lowercased, a qualified argument reduces to its
last segment, non-`[a-z0-9_]` sanitized): `sum(amount)` → `amount_sum`,
`count(user_id)` → `user_id_count`, `sum(users.amount)` → `amount_sum`,
`year(timestamp)` → `timestamp_year`. An explicit `as` always wins. `alias.py`
(`auto_alias`/`effective_alias`) is the one implementation; the AST keeps the
explicit `alias` (or `null`) and `payload_from_ast` bakes the derived name in so
the parser and the engine can never disagree.

Temporals are **derived grouping keys, not aggregates**: alongside an
aggregate, the engine auto-groups them (`year(timestamp)` beside `sum(amount)`
groups by year); alone in the select list they project row-wise
(`month(timestamp)` per row, no GROUP BY).

## What the palette reads

Typing `\` opens the palette; the pure rule behind it is `view_for(...)` in
`anyql/tui/palette.py`, and the language surface it exposes is:

- the twenty clause commands above, plus the app's own actions (`Run`, `Compile`,
  `Data source…`, `History`, and the four that change the workspace: `Results`,
  `SQL` and `Schema` show/hide their pane, `Settings` opens the full-screen
  `\settings` menu) — actions are **not** document commands and never enter the
  AST;
- argument offers: table names for `\from`/`\open`/`\join` and the three set
  operations — this document's `\with` names first, then the active source's
  datasets (a temp table this session made says `temp table` instead of a row
  count) — but only while the clause is still nameless, since `\from events |`
  takes an alias, not a second table; `\union`/`\intersect`/`\except` offer
  `all`/`distinct` before their name is typed and drop them once it is; `\drop`
  offers the temp tables there are to drop. This
  document's open-table columns for `\select`/`\group`/`\order`
  (the cross-dataset pool when the document has no `\from` yet), and for
  `\where` the chain column → capability operator → that column's distinct
  values (numeric values inserted bare, text values quoted), which stop once the
  clause carries a closed value, since one `\where` sets exactly one — and stop
  right after `~`, `!~`, `in` or `not in`, whose operand is typed, not chosen;
- in `\select`, the fields first and then the functions that apply to them: the
  aggregates the backend advertises, the temporal extractions at least one open
  column can carry, and the rank functions. An open call (`sum(`) offers that
  function's argument instead — an aggregate takes any column (`count` also
  takes `*`), a temporal extraction only the dtype families the backend lists
  it under — and accepting `sum(` leaves the popup open on the argument;
- no offers for `\case`, `\limit`, `\with`, `\temp`, or the transaction
  commands — their arguments are typed.

Every offer matches case-insensitively **anywhere** in the name (`tomer` finds
`customer_totals`), ranked exact → prefix → substring with the declaration
order kept inside a rank. The view carries a `phase`: `command` for the word
right after the `\`, `argument` for everything a command takes. The popup is on
wherever the caret sits somewhere the language has something to say: an edit —
typed, inserted from the schema tree, or **accepted** — recomputes the view, so
an accept lands in the offers the same characters typed by hand would have
produced and a clause jump shows the offers at the line it lands on. A bare
caret move only carries an already-open popup along; it never summons one, so
the arrows stay the document's own. Escape toggles the popup — shut while
typing continues, open again on the next Escape — and its dismissal silences
the whole popup; a fresh `\` (or that second Escape) is what brings it back. The
Settings menu's Intellisense switch is the hard off: nothing opens while it is
off, and `ctrl+comma` opens Settings without the palette. A `\where` value
search reads the whole distinct pool the session fetched per column
(`VALUE_POOL_LIMIT`), showing at most `VALUE_SUGGESTIONS` matches.

Accepting an offer replaces the typed span with the command/argument plus a
trailing space; accepting one that would change nothing is not an acceptance, so
Enter keeps meaning a newline. A **clause command** goes further: it asks
`clause_line(doc, line, command)` in `anyql/query/parser.py` for that clause in
the block the caret is editing — a `\with` body is a block of its own, `\open`
is `\from`, and a repeated clause answers with its last line. Finding one, the
accept writes nothing and moves the caret to that line; finding none, it breaks
the line first and the command starts one of its own. These rules are covered in
`tests/test_tui.py` and `tests/test_query.py` (and `view_for` is tested without
a terminal at all).
