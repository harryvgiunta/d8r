# Keyword Tokens and Regex Patterns from parser.py

This file documents all keyword tokens and their regex patterns defined in
`d8r/query/parser.py`.

## Token Definitions

| Token | Regex Pattern | Description |
|---|---|---|
| `_COL` | `[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?` | Column reference: bare (`amount`) or qualified with dot (`users.score`) |
| `_IDENT` | `[A-Za-z_][A-Za-z0-9_]*` | Bare identifier: dataset, alias, CTE/temp/savepoint name |
| `_COL_RE` | `^{_COL}$` | Column reference exact match |
| `_AGG_RE` | `^([A-Za-z_][A-Za-z0-9_]*)\((\*\|{_COL})\)$` | Aggregate function call `name(col)` |
| `_AS_RE` | `^(.*?)\s+as\s+([A-Za-z_][A-Za-z0-9_]*)$` (IGNORECASE) | `expr as alias` pattern |
| `_WHERE_HEAD_RE` | `^({_COL})\s+(not\s+in\|in\|!=\|!~\|>=\|<=\|like\|~\|=\|>\|<)\s*(.*)$` (IGNORECASE) | Where clause head: `col op tail` — the tail is a value, or `( … )` for a subquery |
| `_ORDER_RE` | `^({_COL})(?:\s+(asc\|desc))?$` (IGNORECASE) | Order by: `col [asc\|desc]` |
| `_JOIN_ON_RE` | `^on\s+({_COL})(?:\s*=\s*({_COL}))?$` (IGNORECASE) | Join key tail: `on left [= right]`; either side may be qualified |
| `_SET_OP_RE` | `^(?:(all\|distinct)\s+)?({_IDENT})$` (IGNORECASE) | Union/intersect/except: `[all\|distinct] dataset` — the modifier is split off first, so this matches the name |
| `_CMD_RE` | `^\\([A-Za-z_][A-Za-z0-9_]*)\s*(.*)$` | Command: `\command args` |
| `_CASE_HEAD_RE` | `^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$` | Case head: `alias =` |
| `_OVER_TAIL_RE` | `^\s*(.*?)\s+over\s*\(\s*([^()]*)\s*\)\s*$` (IGNORECASE) | Over frame tail: captures expr and inner content |
| `_RANK_RE` | `^(rank\|dense_rank\|row_number)\(\s*\)$` (IGNORECASE) | Rank function: `rank()`, `dense_rank()`, `row_number()` |
| `_RANK_NAME_RE` | `^(rank\|dense_rank\|row_number)$` (IGNORECASE) | Rank name matching |
| `_PARTITION_RE` | `^\s*partition\s+by\s+([\w.,\s]+?)\s*$` (IGNORECASE \| ASCII) | Partition by clause inside `over()` |
| `_ORDER_TAIL_RE` | `^\s*order\s+by\s+({_COL})(?:\s+(asc\|desc))?\s*$` (IGNORECASE) | Order by inside `over()` frame |
| `_FRAME_HEAD_RE` | `^(rows\|range)\s+between\s+(.+?)\s+and\s+(.+)$` (IGNORECASE \| ASCII) | Window frame: `rows\|range between <bound> and <bound>` |
| `_FRAME_BOUND_RE` | `^(?:unbounded\s+preceding\|([0-9]+)\s+preceding\|current\s+row\|([0-9]+)\s+following\|unbounded\s+following)$` (IGNORECASE \| ASCII) | One frame bound, in its five spellings |
| `_REGEX_FN_RE` | `^(regexp_extract\|regexp_replace)\s*\((.*)\)$` (IGNORECASE \| ASCII) | Regex call: the arguments are split out of group 2 |
| `_WITH_NAME_RE` | `^([A-Za-z_][A-Za-z0-9_]*)$` (IGNORECASE) | Bare-name argument: `\with`, `\temp`, `\drop`, `\savepoint`, `\release` |
| `_LIMIT_RE` | `^[0-9]+$` | Limit value: pure digits |
| `_WHEN_HEAD_RE` | `^when\b` (IGNORECASE \| ASCII) | When head: `when` keyword |
| `_INDENT_RE` | `^\s` (ASCII) | Indent detection: line starting with whitespace |
| `_PAREN_SPLIT_RE` | `[,\s]+` | Split on commas at paren depth 0 |
| `word` | `\b` + `re.escape(keyword)` + `\b` (IGNORECASE \| ASCII) | Generic word boundary matcher for keyword lookup |

## Structural helpers (no regex can carry these)

| Helper | Shape it reads |
|---|---|
| `take_paren` | `( … ) rest` — the balanced, quote-aware body of an inline subquery and what follows it |
| `split_commands` | An inline body's commands, cut at every `\` at depth 0 outside quotes |
| `parse_subquery` | An inline body as a `QueryAST` of its own, every node on the clause's line |
| `parse_source` | `<dataset> [as] <alias>` or `( … ) [as] <alias>` — the two source shapes `\from`/`\join` take |
| `parse_frame_bounds` | `rows\|range between …` → the two canonical bound strings |
| `parse_regex_call` | The argument list of `regexp_extract`/`regexp_replace` |
| `keyword_positions` | Word-boundary keyword positions outside quotes and parens — how `\join` finds its own `on`, and `\case` its `when`/`then`/`else` |

## Sets the parser owns

| Set | Members | Meaning |
|---|---|---|
| `NEEDS_ARGS` | `from open join union intersect except select where group order limit case with temp drop savepoint release` | A command that got no argument is `\<cmd> expects arguments` |
| `SET_OP_COMMANDS` | `union intersect except` | The three set operations |
| `SET_OP_MODIFIERS` | `all distinct` | The modifier each of them takes first; a bare one names nothing |
| `COMMAND_ALIASES` | `open → from`, `unique → distinct` | Clause lookup treats both spellings as the same clause within a query block |
| `SUBQUERY_OPS` | `in`, `not in` | `\where` operators whose operand must be an inline subquery |
| `REGEX_OPS` | `~`, `!~` | POSIX match and its negation, in `\where` and `\case` conditions |
| `TX_COMMANDS` | `begin commit rollback savepoint release` | Transaction commands, mapped to their `kind`; `\rollback to <name>` is `rollback_to` |
| `TX_BARE` | `begin commit` | The transaction commands that take no argument at all |

## Notes

- All patterns using `_COL` inline-expand the `_COL` definition, producing a combined regex.
- Most patterns use `re.IGNORECASE` flag for case-insensitive matching.
- `_PARTITION_RE`, `_FRAME_HEAD_RE`, `_FRAME_BOUND_RE`, `_REGEX_FN_RE`, `_WHEN_HEAD_RE` and `_INDENT_RE` use `re.ASCII` as well.
- `_WHERE_HEAD_RE` replaces the older `_WHERE_RE`: the tail may be empty while a
  line is being typed, and both `\where` and `\case` read their conditions
  through it (`\case` rejects the subquery operators, which have no operand
  there).
- `\distinct` and `\unique` take no arguments and set the same row-deduplication
  flag. They use command dispatch, not a new expression regex.
- `_JOIN_ON_RE` is matched against the `on …` tail alone: `\join` cuts its own
  argument at the last top-level `on` first, so a dataset, an inline subquery
  body, an `as` alias and `on` can never be mistaken for one another.
- `_OVER_TAIL_RE` captures the expression and the content inside `over()`;
  `parse_over_frame` reads `partition by`, `order by` and an optional
  `rows|range between …` out of that content.
- `_AS_RE` uses non-greedy `.*?` to match up to the first `as`.
- `_SET_OP_RE` has 2 capture groups: the optional modifier and the table name;
  a bare modifier (`\union all`) matches with `all` as the *name*, so the parser
  rejects that shape explicitly instead of looking up a dataset called `all`.
- `split_top` skips quoted strings, so a comma inside a pattern
  (`regexp_replace(p, "/p/[0-9]+", "/a,b")`) stays in its argument.
- `_LIMIT_RE` matches pure ASCII digit strings (since Python `\d` is broader than TS `/^\d+$/`).
