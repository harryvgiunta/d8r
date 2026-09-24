# Keyword Tokens and Regex Patterns from parser.py

This file documents all keyword tokens and their regex patterns defined in
`anyql/query/parser.py`.

## Token Definitions

| Token | Regex Pattern | Description |
|---|---|---|
| `_COL` | `[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?` | Column reference: bare (`amount`) or qualified with dot (`users.score`) |
| `_COL_RE` | `^{_COL}$` | Column reference exact match |
| `_AGG_RE` | `^([A-Za-z_][A-Za-z0-9_]*)\(({_COL})\)$` | Aggregate function call `name(col)` |
| `_AS_RE` | `^(.*?)\s+as\s+([A-Za-z_][A-Za-z0-9_]*)$` (IGNORECASE) | `expr as alias` pattern |
| `_WHERE_RE` | `^({_COL})\s*(=|!=|>=|<=|>|<|like)\s*(.+)$` (IGNORECASE) | Where clause: `col op value` |
| `_ORDER_RE` | `^({_COL})(?:\s+(asc|desc))?$` (IGNORECASE) | Order by: `col [asc|desc]` |
| `_JOIN_RE` | `^([A-Za-z_][A-Za-z0-9_]*)` `(?:\s+(?:as\s+)?([A-Za-z_][A-Za-z0-9_]*))?` `\s+on\s+` `([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)` `(?:\s*=\s*([A-Za-z_][A-Za-z0-9_]*))?$` (IGNORECASE) | Join: `\join dataset [as alias] on left [= right]` |
| `_FROM_RE` | `^([A-Za-z_][A-Za-z0-9_]*)(?:\s+(?:as\s+)?([A-Za-z_][A-Za-z0-9_]*))?$` (IGNORECASE) | From/open: `dataset [as alias]` |
| `_CMD_RE` | `^\\([A-Za-z_][A-Za-z0-9_]*)\s*(.*)$` | Command: `\command args` |
| `_CASE_HEAD_RE` | `^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$` | Case head: `alias =` |
| `_OVER_TAIL_RE` | `^\s*(.*?)\s+over\s*\(\s*([^()]*)\s*\)\s*$` (IGNORECASE) | Over frame tail: captures expr and inner content |
| `_RANK_RE` | `^(rank|dense_rank|row_number)\(\s*\)$` (IGNORECASE) | Rank function: `rank()`, `dense_rank()`, `row_number()` |
| `_RANK_NAME_RE` | `^(rank|dense_rank|row_number)$` (IGNORECASE) | Rank name matching |
| `_PARTITION_RE` | `^\s*partition\s+by\s+([\w.,\s]+?)\s*$` (IGNORECASE \| ASCII) | Partition by clause inside `over()` |
| `_ORDER_TAIL_RE` | `^\s*order\s+by\s+({_COL})(?:\s+(asc|desc))?\s*$` (IGNORECASE) | Order by inside `over()` frame |
| `_WITH_NAME_RE` | `^([A-Za-z_][A-Za-z0-9_]*)$` (IGNORECASE) | CTE header: `\with <name>` |
| `_LIMIT_RE` | `^[0-9]+$` | Limit value: pure digits |
| `_WHEN_HEAD_RE` | `^when\b` (IGNORECASE \| ASCII) | When head: `when` keyword |
| `_INDENT_RE` | `^\s` (ASCII) | Indent detection: line starting with whitespace |
| `_PAREN_SPLIT_RE` | `[,\s]+` | Split on commas at paren depth 0 |
| `word` | `\b` + `re.escape(keyword)` + `\b` (IGNORECASE \| ASCII) | Generic word boundary matcher for keyword lookup |

## Notes

- All patterns using `_COL` inline-expand the `_COL` definition, producing a combined regex.
- Most patterns use `re.IGNORECASE` flag for case-insensitive matching.
- `_PARTITION_RE` uses both `re.IGNORECASE | re.ASCII`.
- `_WHEN_HEAD_RE` uses `re.IGNORECASE | re.ASCII`.
- `_JOIN_RE` has 4 capture groups: dataset, optional alias, left identifier, optional right identifier.
- `_OVER_TAIL_RE` captures the expression and the content inside `over()`.
- `_AS_RE` uses non-greedy `.*?` to match up to the first `as`.
- `_WHERE_RE` supports `=`, `!=`, `>=`, `<=`, `>`, `<`, `like` operators.
- `_LIMIT_RE` matches pure ASCII digit strings (since Python `\d` is broader than TS `/^\d+$/`).
