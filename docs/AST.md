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
| `\from` | `<dataset> [as] alias` | sets `from` (a later `\from`/`\open` replaces it) |
| `\open` | `<dataset> [as] alias` | identical to `\from` — the dataset-source spelling |
| `\join` | `<dataset> [as] alias on col[ = col]` | appends to `joins` (repeatable; always INNER) |
| `\union` | `[all\|distinct] <dataset\|cte>` | appends to `setOps` (repeatable; distinct unless `all`) |
| `\intersect` | `[all\|distinct] <dataset\|cte>` | appends to `setOps` (repeatable; keeps shared rows) |
| `\except` | `[all\|distinct] <dataset\|cte>` | appends to `setOps` (repeatable; subtracts the operand) |
| `\select` | expression | appends to `select` |
| `\where` | `column op value` | sets `where` (a later `\where` replaces) |
| `\group` | `column[ column…]` | appends to `groupBy` |
| `\order` | `target [asc\|desc]` | appends to `orderBy` (default `asc`) |
| `\case` | `<alias> = when … then … [else …]` | appends a computed column (repeatable) |
| `\limit` | non-negative integer | sets `limit` (a later `\limit` wins) |

Commands are case-insensitive (`\FROM` is `\from`); the command name is
lowercased into the AST. A line that does not start with `\` is the error
`` not a command — lines start with \ ``; an unknown name is
`unknown command "\<name>"`; a command that needs arguments and got none is
`\<cmd> expects arguments` (`from`, `open`, `join`, `union`, `intersect`,
`except`, `select`, `where`, `group`, `order`, `limit`, `case`, `with`).

Argument rules:

- No commas are needed in lists: `\group customer_id region` is two group terms
  (comma-separated input works too, as does any mix: `\group user_id, region
  status`).
- Anything with a space is a wrapped string: `\where name = John Smith` stores
  the value `John Smith` without requiring quotes. Quotes are optional and one
  matching layer is stripped when present (`"paid"` and `'paid'` both store
  `paid`); a value containing a quote character keeps it.
- Expressions in `\select` are one of: a plain column (`customer_id`), a
  **star** (`*`, see below), an aggregate call over a column (`sum(amount)`),
  a **temporal extraction** over a temporal column (`year(timestamp)`,
  `hour(e.occurred_at)`), or a **rank call** (`rank()`, `dense_rank()`,
  `row_number()` — no argument). Any aggregate or rank call may carry a
  trailing **`over ( … )` window frame** (see below). Each item optionally ends
  with `as alias`, case-insensitively. Comma-separated expressions on one line
  are separate select items (`\select user_id, sum(amount) as total` yields
  two), split only at paren depth 0 — `count(a, b)` stays one item.
- `\join users on user_id` joins dataset `users` on the shared column
  `user_id` (left side = right side). `\join users on user_id = id` names both
  sides. Joins apply in document order, each onto the result of everything
  before it, always as an INNER join. A malformed clause (missing `on`, or a
  *qualified* right-hand side — only the left may be identifier-qualified) is
  ``\join expects `<dataset> [as] <alias> on <col>[ = <col>]` ``.
- A bare second identifier after the dataset is a **table alias**
  (`\from events e`, `\join users as u on user_id`); the `as` keyword is
  optional. Duplicate identifiers among open tables are the error
  `duplicate table identifier "x"`.
- Type compatibility is **not** the parser's business: a `year` on a float
  column parses, and the engine refuses it at build time
  (`unknown function: 'year' for float64`). Completion keeps it from happening
  interactively by filtering on the capability map.

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
  and **before** `\order`/`\limit`: a trailing `\order`/`\limit` orders and caps
  the merged result, which is SQL's own reading of the document. A block is
  still one query — an operand is a name, never an inline sub-query, so anything
  more involved goes in a `\with` body.

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
  arguments and window `partition by`/`order by`), `\where`, `\group`,
  `\order`, `\case` conditions, and the left-hand side of a `\join` — where
  only identifiers established *before* that clause count (the clause's own
  alias does not). With no `\from` at all the message lists `(no \from)`.
- An `\order` target that matches nothing is **not** an error: it parses with
  `resolvesTo: null` and the engine refuses it at run time
  (`unknown orderBy target: 'ghost'`).

## Shape

`QueryAST.to_json()` emits the documented wire shape — exactly the keys
`spec/canonical-query.ast.json` carries (Python fields are snake_case;
`with_`, `from_`, `group_by`, `order_by` map to `with`, `from`, `groupBy`,
`orderBy`):

```jsonc
{
  "with": [{ "line": 1, "name": "recent", "body": { /* full QueryAST; errors always [] */ } }],
  "from": { "line": 4, "table": "recent", "alias": null },        // or null
  "joins": [{ "line": 2, "dataset": "users", "alias": "u", "left": "user_id", "right": "id" }],
  "setOps": [{ "line": 10, "op": "union", "dataset": "archived", "distinct": true }],  // op: union|intersect|except
  "select": [{
    "line": 7, "raw": "sum(amount) over (partition by customer_id) as customer_total",
    "column": null,          // plain column, else null
    "star": false,           // \select *
    "aggregate": { "fn": "sum", "arg": "amount" },   // or null
    "temporal": null,        // { "fn": "month", "arg": "placed_at" } or null
    "rank": null,            // { "fn": "rank" } or null
    "window": { "partitionBy": ["customer_id"], "order": null },  // or null
    "alias": "customer_total"   // explicit alias, else null
  }],
  "where": { "line": 3, "raw": "status = \"paid\"", "column": "status", "op": "=", "value": "paid" },  // or null
  "groupBy": [{ "line": 5, "column": "customer_id" }],
  "orderBy": [{ "line": 10, "target": "customer_total", "direction": "desc", "resolvesTo": 3 }],
  "cases": [{
    "line": 9, "raw": "flag = when amount > 100 then \"high\" else \"low\"",
    "alias": "flag",
    "whens": [{ "column": "amount", "op": ">", "value": "100", "then": "high" }],
    "else": "low"            // or null
  }],
  "limit": 5,                // or null
  "errors": [{ "line": 2, "message": "…" }]
}
```

- `line` is a 1-based document line number.
- `direction` is `"asc"` or `"desc"`; the default is `asc`. `\limit` takes the
  last non-negative integer; anything else is
  `limit must be a non-negative integer` and leaves the previous value in place.
- `value`/`then`/`else` strings are stored with surrounding quotes stripped;
  `raw` keeps the line as typed (the `\case` raw excludes the command word).
- `setOps[i].distinct` is SQL's default (`true`) unless the clause said `all`.
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
aggregate arguments, window frames, and `\case` conditions) are either:

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

- the thirteen clause commands above, plus the app's own actions (`Run`, `Compile`,
  `Data source…`, `History`, and the four that change the workspace: `Results`,
  `SQL` and `Schema` show/hide their pane, `Settings` opens the full-screen
  `\settings` menu) — actions are **not** document commands and never enter the
  AST;
- argument offers: table names for `\from`/`\open`/`\join` and the three set
  operations — this document's `\with` names first, then the active source's
  datasets — but only while the clause is still nameless, since `\from events |`
  takes an alias, not a second table; `\union`/`\intersect`/`\except` offer
  `all`/`distinct` before their name is typed and drop them once it is. This
  document's open-table columns for `\select`/`\group`/`\order`
  (the cross-dataset pool when the document has no `\from` yet), and for
  `\where` the chain column → capability operator → that column's distinct
  values (numeric values inserted bare, text values quoted), which stop once the
  clause carries a closed value, since one `\where` sets exactly one;
- in `\select`, the fields first and then the functions that apply to them: the
  aggregates the backend advertises, the temporal extractions at least one open
  column can carry, and the rank functions. An open call (`sum(`) offers that
  function's argument instead — an aggregate takes any column (`count` also
  takes `*`), a temporal extraction only the dtype families the backend lists
  it under — and accepting `sum(` leaves the popup open on the argument;
- no offers for `\case`, `\limit`, or `\with` — their arguments are typed.

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
