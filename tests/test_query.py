"""Unit suite for the pure language layer: parser regimes (empty vs loaded
schema), CTE validation, select-expression classification, window frames,
`\\case` parsing, joins, order resolution, and the AST -> engine payload
mapping. No DOM, no backend.

Ported from the earlier TypeScript suite plus the contract cases in
`docs/AST.md` (pinned by the `spec/canonical-query.*` fixture pair). The
completion/formatting cases of that suite are not part of this layer and are not
ported here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from anyql.query import (  # noqa: E402  (repo root added to sys.path above)
    CaseBranch,
    ColumnDef,
    TableDef,
    auto_alias,
    clause_line,
    parse_query,
    payload_from_ast,
    set_schema_state,
    split_top,
    unquote,
)

TABLES = [
    TableDef(
        "events",
        "events",
        [
            ColumnDef("user_id", "int64", ""),
            ColumnDef("amount", "float64", ""),
            ColumnDef("status", "string", "", ["active", "paid"]),
            ColumnDef("ts", "timestamp", ""),
        ],
    ),
    TableDef(
        "users",
        "users",
        [
            ColumnDef("user_id", "int64", ""),
            ColumnDef("score", "float64", ""),
        ],
    ),
]


@pytest.fixture(autouse=True)
def _empty_registry():
    """Every test starts (and ends) with the schema registry empty."""
    set_schema_state([])
    yield
    set_schema_state([])


@pytest.fixture
def loaded():
    set_schema_state(TABLES)


def messages(ast) -> list[str]:
    return [e.message for e in ast.errors]


def has_error(ast, needle: str) -> bool:
    return any(needle in m for m in messages(ast))


def sort_deep(value):
    if isinstance(value, list):
        return [sort_deep(v) for v in value]
    if isinstance(value, dict):
        return {k: sort_deep(value[k]) for k in sorted(value)}
    return value


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


CANONICAL_DOC = read("spec/canonical-query.anyql").replace("\r\n", "\n")


# --- canonical fixture (empty registry — the parser-only regime) -------------


def test_canonical_fixture_deep_equals_checked_in_ast():
    ast = parse_query(CANONICAL_DOC)
    assert ast.errors == []
    want = json.loads(read("spec/canonical-query.ast.json"))
    assert sort_deep(ast.to_json()) == sort_deep(want)


def test_crlf_documents_parse_identically():
    # The editor may hand over CRLF text; only line splitting differs.
    crlf = CANONICAL_DOC.replace("\n", "\r\n")
    assert sort_deep(parse_query(crlf).to_json()) == sort_deep(parse_query(CANONICAL_DOC).to_json())


def test_lines_are_trimmed_before_dispatch():
    ast = parse_query("   \\from events   \n\t\\select user_id\t\n\\limit 2")
    assert ast.errors == []
    assert ast.from_.table == "events"
    assert ast.select[0].raw == "user_id"


# --- empty vs loaded registry -----------------------------------------------


def test_empty_registry_accepts_any_dataset_and_qualified_ref():
    ast = parse_query("\\from whatever\n\\join more as m on user_id = id\n\\select m.thing\n\\limit 2")
    assert ast.errors == []


def test_loaded_registry_flags_unknown_table(loaded):
    ast = parse_query("\\from nope\n\\limit 2")
    assert messages(ast) == ['unknown table "nope" — loaded datasets: events, users']
    assert ast.errors[0].line == 1

    joined = parse_query("\\from events\n\\join nope on user_id\n\\limit 2")
    assert messages(joined) == ['unknown table "nope" — loaded datasets: events, users']
    assert joined.errors[0].line == 2


def test_loaded_registry_flags_unknown_qualified_column(loaded):
    ast = parse_query("\\from events as e\n\\select e.user_id\n\\limit 2")
    assert ast.errors == []

    # Aliasing is strict: the dataset name of an aliased table is unknown.
    ast = parse_query("\\from events as e\n\\select events.user_id\n\\limit 2")
    assert messages(ast) == [
        'unknown column "events.user_id" — "events" is not an open table: e'
    ]

    ast = parse_query("\\from events as e\n\\select u.score\n\\limit 2")
    assert messages(ast) == ['unknown column "u.score" — "u" is not an open table: e']


def test_qualified_ref_without_from_lists_the_placeholder():
    set_schema_state(TABLES)
    ast = parse_query("\\select u.score\n\\limit 2")
    assert messages(ast) == ['unknown column "u.score" — "u" is not an open table: (no \\from)']


def test_qualified_refs_are_checked_in_every_clause(loaded):
    ast = parse_query(
        "\\from events as e\n"
        "\\select sum(u.amount)\n"
        "\\where x.col = 1\n"
        "\\group e.user_id\n"
        "\\order e.user_id\n"
        "\\case c = when y.col > 1 then 2\n"
        "\\limit 2"
    )
    assert messages(ast) == [
        'unknown column "u.amount" — "u" is not an open table: e',
        'unknown column "x.col" — "x" is not an open table: e',
        'unknown column "y.col" — "y" is not an open table: e',
    ]
    assert [e.line for e in ast.errors] == [2, 3, 6]


def test_window_partition_and_order_refs_are_checked(loaded):
    ast = parse_query("\\from events as e\n\\select sum(amount) over (partition by z.a order by z.b)\n\\limit 2")
    assert messages(ast) == [
        'unknown column "z.a" — "z" is not an open table: e',
        'unknown column "z.b" — "z" is not an open table: e',
    ]


# --- typing-line error suppression ------------------------------------------


def test_last_non_empty_line_is_the_typing_line(loaded):
    # Half-typed commands on the very last content line are normal, not errors.
    assert parse_query("\\from nope").errors == []
    assert parse_query("\\with a").errors == []
    assert parse_query("\\where amount =").errors == []
    assert parse_query("\\limit x").errors == []
    assert parse_query("\\case f = when ").errors == []
    assert parse_query("\\select sum(").errors == []
    # Trailing blank lines don't move the typing line off the last command.
    assert parse_query("\\from nope\n\n").errors == []
    # A settled line before the typing line still reports.
    assert messages(parse_query("\\from nope\n\\limit 2")) == [
        'unknown table "nope" — loaded datasets: events, users'
    ]


def test_half_typed_select_keeps_the_item_without_classifying_it():
    ast = parse_query("\\from events\n\\select sum(")
    assert ast.errors == []
    assert len(ast.select) == 1
    assert ast.select[0].raw == "sum("
    assert (ast.select[0].column, ast.select[0].aggregate, ast.select[0].window) == (None, None, None)


# --- `\with` CTE blocks ------------------------------------------------------


def test_with_body_parses_as_a_full_query_with_absolute_lines():
    ast = parse_query('\\with recent\n  \\from orders\n  \\where status = "paid"\n\\from recent\n\\select user_id')
    assert ast.errors == []
    with_clause = ast.with_[0]
    assert (with_clause.line, with_clause.name) == (1, "recent")
    body = with_clause.body
    assert (body.from_.line, body.from_.table) == (2, "orders")
    assert (body.where.line, body.where.value) == (3, "paid")
    assert body.errors == []
    assert (ast.from_.line, ast.from_.table) == (4, "recent")


def test_main_from_and_join_may_reference_earlier_ctes(loaded):
    ast = parse_query("\\with recent\n  \\from events\n\\from recent\n\\select user_id")
    assert ast.errors == []
    assert ast.from_.table == "recent"

    ast = parse_query("\\with recent\n  \\from events\n\\from users\n\\join recent on user_id\n\\select score")
    assert ast.errors == []


def test_forward_reference_to_a_later_cte_is_unknown_table(loaded):
    ast = parse_query("\\from recent\n\\with recent\n  \\from events")
    assert has_error(ast, 'unknown table "recent"')
    assert [e.line for e in ast.errors] == [1]


def test_nested_cte_in_a_body_is_rejected(loaded):
    ast = parse_query("\\with a\n  \\from events\n  \\with b\n    \\from users")
    assert has_error(ast, "nested CTEs are not supported")
    assert [e.line for e in ast.errors] == [3]


def test_duplicate_cte_name_is_rejected(loaded):
    ast = parse_query("\\with a\n  \\from events\n\\with a\n  \\from users")
    assert messages(ast) == ['duplicate CTE name "a"']
    assert [e.line for e in ast.errors] == [3]


def test_cte_name_shadowing_a_dataset_is_flagged(loaded):
    ast = parse_query("\\with events\n  \\from users")
    assert messages(ast) == ['CTE name "events" shadows dataset "events"']
    assert [e.line for e in ast.errors] == [1]
    # Nothing to shadow before the registry loads.
    set_schema_state([])
    assert parse_query("\\with events\n  \\from users").errors == []


def test_qualified_refs_resolve_inside_a_cte_body(loaded):
    ast = parse_query("\\with a\n  \\from events as e\n  \\select e.user_id")
    assert ast.errors == []

    ast = parse_query("\\with a\n  \\from events as e\n  \\select events.user_id\n\\limit 2")
    assert messages(ast) == [
        'unknown column "events.user_id" — "events" is not an open table: e'
    ]
    assert [e.line for e in ast.errors] == [3]


def test_cte_body_sees_earlier_ctes(loaded):
    ast = parse_query("\\with a\n  \\from events\n\\with b\n  \\from a\n  \\select user_id")
    assert ast.errors == []
    assert ast.with_[1].body.from_.table == "a"


def test_with_expects_a_bare_name_and_a_body(loaded):
    ast = parse_query("\\with 1bad\n  \\from events\n\\limit 2")
    assert messages(ast) == ["\\with expects a bare CTE name"]
    ast = parse_query("\\with a\n\\limit 2")
    assert messages(ast) == ["\\with a expects an indented body"]

    # A blank-only indented block is still no body.
    ast = parse_query("\\with a\n   \n\\limit 2")
    assert messages(ast) == ["\\with a expects an indented body"]


def test_body_errors_bubble_to_the_document_with_absolute_lines(loaded):
    ast = parse_query("\\with a\n  \\from events\n  \\select bogus!\n\\limit 2")
    assert messages(ast) == ['cannot parse expression "bogus!"']
    assert [e.line for e in ast.errors] == [3]
    assert ast.with_[0].body.errors == []
    assert ast.with_[0].body.select[0].line == 3


def test_blank_line_stays_in_the_block_only_when_it_continues(loaded):
    # A blank line followed by an indented line is part of the body ...
    ast = parse_query("\\with a\n  \\from events\n\n  \\select user_id\n\\limit 2")
    assert ast.with_[0].body.select[0].line == 4
    assert ast.limit == 2
    assert ast.errors == []

    # ... followed by a flush line it ends the block.
    ast = parse_query("\\with a\n  \\from events\n\n\\limit 2")
    assert ast.with_[0].body.select == []
    assert ast.with_[0].body.limit is None
    assert ast.limit == 2
    assert ast.errors == []


def test_clause_line_reads_the_block_a_line_belongs_to():
    """The palette's clause lookup: block-scoped, `\\open` is `\\from`, last wins."""
    doc = (
        "\\with recent\n"
        "  \\from orders\n"
        "  \\select customer_id\n"
        '  \\where status = "paid"\n'
        "\\from recent\n"
        "\\select user_id\n"
        "\\select amount\n"
        "\\where amount > 1"
    )
    # At the top level: its own clauses exist, a repeated clause answers with the
    # last line that carries it, and a clause it lacks answers with nothing.
    assert clause_line(doc, 8, "where") == 8
    assert clause_line(doc, 8, "select") == 7
    assert clause_line(doc, 8, "from") == 5
    assert clause_line(doc, 8, "limit") is None
    assert clause_line(doc, 6, "with") == 1  # the header is the block's clause

    # Inside the body: the body's clauses, never the document's.
    assert clause_line(doc, 4, "from") == 2
    assert clause_line(doc, 4, "select") == 3
    assert clause_line(doc, 4, "where") == 4
    assert clause_line(doc, 4, "group") is None

    # A body's clause is not the document's: the body is a block of its own.
    scoped = '\\with recent\n  \\from orders\n  \\where status = "paid"\n\\from recent'
    assert clause_line(scoped, 4, "where") is None
    assert clause_line(scoped, 4, "from") == 4
    assert clause_line(scoped, 3, "where") == 3

    # Blank lines continue the body only while the next non-blank line is indented.
    spaced = "\\with a\n  \\from events\n\n  \\select user_id\n\\limit 2"
    assert clause_line(spaced, 3, "from") == 2
    assert clause_line(spaced, 3, "limit") is None
    assert clause_line(spaced, 5, "limit") == 5

    # `\open` is the same clause as `\from`, in either spelling.
    assert clause_line("\\open events", 1, "from") == 1
    assert clause_line("\\open events", 1, "open") == 1

    # A line past the end (the palette's caret on a fresh line) still resolves.
    assert clause_line(doc, 99, "where") == 8


# --- open-table identifiers --------------------------------------------------


def test_duplicate_table_identifier_is_rejected(loaded):
    ast = parse_query("\\from events as x\n\\join users as x on user_id\n\\limit 2")
    assert messages(ast) == ['duplicate table identifier "x"']
    assert [e.line for e in ast.errors] == [2]

    # An unaliased dataset colliding with an explicit alias counts too.
    ast = parse_query("\\from events\n\\join users as events on user_id\n\\limit 2")
    assert messages(ast) == ['duplicate table identifier "events"']

    # Same rule on the typing line: suppressed.
    assert parse_query("\\from events as x\n\\join users as x on user_id").errors == []


# --- select expressions ------------------------------------------------------


def test_aggregate_temporal_and_rank_classification():
    ast = parse_query(
        "\\from events\n"
        "\\select sum(amount) as total, month(ts), rank() over (order by ts desc)\n"
        "\\limit 2"
    )
    assert ast.errors == []
    total, month_col, rank_col = ast.select
    assert total.raw == "sum(amount) as total"
    assert (total.aggregate.fn, total.aggregate.arg, total.alias) == ("sum", "amount", "total")
    assert (month_col.temporal.fn, month_col.temporal.arg, month_col.alias) == ("month", "ts", None)
    assert rank_col.rank.fn == "rank"
    assert rank_col.window.partition_by == []
    assert (rank_col.window.order.column, rank_col.window.order.direction) == ("ts", "desc")


def test_unknown_function_is_reported():
    ast = parse_query("\\select median(amount)\n\\limit 2")
    assert messages(ast) == ['unknown function "median"']


def test_rank_names_with_arguments_are_rejected():
    for fn in ("rank", "dense_rank", "row_number"):
        ast = parse_query(f"\\select {fn}(amount)\n\\limit 2")
        assert messages(ast) == [f'"{fn}" takes no arguments — write {fn}() over ( ... )']


def test_unparseable_select_expression_is_reported():
    ast = parse_query("\\select amount + 1\n\\limit 2")
    assert messages(ast) == ['cannot parse expression "amount + 1"']


def test_rank_requires_the_frame_and_its_order():
    ast = parse_query("\\select rank()\n\\limit 2")
    assert messages(ast) == ["rank() requires over ( ... )"]
    assert ast.select[0].rank.fn == "rank"
    assert ast.select[0].window is None

    ast = parse_query("\\select rank() over (partition by user_id)\n\\limit 2")
    assert messages(ast) == ["rank() requires order by inside over (...)"]
    assert ast.select[0].window.partition_by == ["user_id"]


def test_temporals_cannot_carry_a_window():
    ast = parse_query("\\select year(ts) over (order by ts)\n\\limit 2")
    assert messages(ast) == ['"year" is not a window function — use sum/avg/count/min/max']
    # Parsed structurally like an aggregate; the window stays attached.
    assert (ast.select[0].temporal.fn, ast.select[0].temporal.arg) == ("year", "ts")
    assert ast.select[0].window.order.column == "ts"


def test_a_plain_column_cannot_carry_a_window():
    ast = parse_query("\\select amount over (order by ts)\n\\limit 2")
    assert messages(ast) == ["a plain column cannot carry over (...) — wrap it in a function"]
    assert ast.select[0].column == "amount"


# --- `over ( … )` frames -----------------------------------------------------


def test_over_frame_partition_and_order():
    ast = parse_query("\\select sum(amount) over (partition by customer_id, region)\n\\limit 2")
    assert ast.errors == []
    assert ast.select[0].window.partition_by == ["customer_id", "region"]
    assert ast.select[0].window.order is None
    assert ast.select[0].aggregate.fn == "sum"

    ast = parse_query("\\select sum(amount) over (order by placed_at desc)\n\\limit 2")
    assert ast.errors == []
    assert (ast.select[0].window.order.column, ast.select[0].window.order.direction) == ("placed_at", "desc")

    ast = parse_query("\\select sum(amount) over (order by placed_at)\n\\limit 2")
    assert ast.select[0].window.order.direction == "asc"

    ast = parse_query("\\select sum(amount) over (partition by customer_id order by placed_at)\n\\limit 2")
    assert ast.select[0].window.partition_by == ["customer_id"]
    assert ast.select[0].window.order.column == "placed_at"


def test_over_frame_needs_partition_or_order():
    err = "over (...) needs partition by <cols> and/or order by <col> [asc|desc]"
    for expr in ("sum(amount) over ()", "sum(amount) over (partition by)", "sum(amount) over (anything)"):
        ast = parse_query(f"\\select {expr}\n\\limit 2")
        assert messages(ast) == [err], expr
        # The item is kept, unclassified, exactly as the TS parser does.
        assert ast.select[0].raw == expr
        assert (ast.select[0].aggregate, ast.select[0].window) == (None, None)


def test_over_frame_takes_at_most_one_order():
    ast = parse_query("\\select sum(amount) over (order by a order by b)\n\\limit 2")
    assert messages(ast) == ["over (...) needs partition by <cols> and/or order by <col> [asc|desc]"]


def test_over_is_case_insensitive_and_tolerates_spacing():
    ast = parse_query("\\select sum(amount) OVER (PARTITION BY customer_id ORDER BY placed_at DESC) AS t\n\\limit 2")
    assert ast.errors == []
    assert ast.select[0].alias == "t"
    assert ast.select[0].window.partition_by == ["customer_id"]
    assert ast.select[0].window.order.direction == "desc"


# --- star select -------------------------------------------------------------


def test_star_parses_to_a_star_item_and_the_payload_carries_star():
    ast = parse_query("\\from events\n\\select *")
    assert ast.errors == []
    assert len(ast.select) == 1
    assert ast.select[0].star is True
    assert ast.select[0].column is None
    payload = payload_from_ast(ast)
    assert payload["select"][0] == {
        "column": None,
        "star": True,
        "aggregate": None,
        "temporal": None,
        "rank": None,
        "window": None,
        "alias": None,
    }


def test_star_rejects_an_alias_and_a_frame():
    assert has_error(parse_query("\\from events\n\\select * as x\n\\limit 2"), "`*` cannot take an alias")
    assert has_error(
        parse_query("\\from events\n\\select * over (order by amount)\n\\limit 2"),
        "`*` cannot carry over (...)",
    )
    # Both bad lines are settled (a later line follows), so both report.
    ast = parse_query("\\from events\n\\select * as x\n\\limit 2")
    assert [e.line for e in ast.errors] == [2]


def test_select_user_id_star_parses_both_items_in_order():
    ast = parse_query("\\from events\n\\select user_id, *")
    assert ast.errors == []
    assert [("*" if s.star else s.column) for s in ast.select] == ["user_id", "*"]


# --- `\case` -----------------------------------------------------------------


def test_case_parses_multiple_whens_and_else():
    ast = parse_query(
        '\\case tier = when amount > 100 then "high" when amount > 10 then "mid" else "low"\n\\limit 2'
    )
    assert ast.errors == []
    case = ast.cases[0]
    assert (case.line, case.alias) == (1, "tier")
    assert case.raw == 'tier = when amount > 100 then "high" when amount > 10 then "mid" else "low"'
    assert [(w.column, w.op, w.value, w.then) for w in case.whens] == [
        ("amount", ">", "100", "high"),
        ("amount", ">", "10", "mid"),
    ]
    assert case.else_ == "low"


def test_case_without_else_defaults_to_null():
    ast = parse_query('\\case flag = when status = "paid" then 1\n\\limit 2')
    assert ast.errors == []
    assert ast.cases[0].else_ is None
    assert ast.cases[0].whens[0].then == "1"


def test_quoted_keywords_never_split_a_case_branch():
    # The quoted `else` belongs to the then-value, not to the clause.
    ast = parse_query('\\case tag = when status = "x" then "else" else "y"\n\\limit 2')
    assert ast.errors == []
    assert ast.cases[0].whens == [CaseBranch("status", "=", "x", "else")]
    assert ast.cases[0].else_ == "y"

    # ... and a quoted `when`/`then` in the condition value stays inside it.
    ast = parse_query('\\case tag = when name = "when a then b" then "x" else "y"\n\\limit 2')
    assert ast.errors == []
    assert ast.cases[0].whens[0].value == "when a then b"
    assert ast.cases[0].whens[0].then == "x"
    assert ast.cases[0].else_ == "y"

    # A quoted `then` in the then-value doesn't count as the branch separator.
    ast = parse_query('\\case tag = when amount > 1 then "a then b"\n\\limit 2')
    assert ast.errors == []
    assert ast.cases[0].whens[0].then == "a then b"


def test_case_rejects_missing_pieces():
    err = "\\case expects `<alias> = when <col> <op> <value> then <value> [when …] [else <value>]`"
    for doc in (
        "\\case tier = when amount > 100 else 2",  # no then
        "\\case tier = when amount > 100 then",  # empty then
        "\\case tier = when amount > 100 then 1 else",  # empty else
        "\\case tier = when amount > 100 then 1 when amount > 10",  # second branch has no then
        "\\case tier when amount > 100 then 1",  # no `= when`
        "\\case = when amount > 100 then 1",  # no alias
        "\\case tier = amount > 100 then 1",  # no `when`
        "\\case tier = when amount 100 then 1",  # no operator
    ):
        ast = parse_query(f"{doc}\n\\limit 2")
        assert messages(ast) == [err], doc
        assert ast.cases == []


def test_case_operators_and_values_follow_where_rules():
    ast = parse_query("\\case flag = when name LIKE '%paid%' then 'yes' else 'no'\n\\limit 2")
    assert ast.errors == []
    when = ast.cases[0].whens[0]
    assert (when.op, when.value, when.then, ast.cases[0].else_) == ("like", "%paid%", "yes", "no")


# --- `\where` ----------------------------------------------------------------


def test_where_lowercases_the_operator_including_like():
    ast = parse_query("\\where amount LIKE 5\n\\limit 2")
    assert ast.errors == []
    assert (ast.where.line, ast.where.raw) == (1, "amount LIKE 5")
    assert (ast.where.column, ast.where.op, ast.where.value) == ("amount", "like", "5")

    for op in ("!=", ">", ">=", "<", "<=", "="):
        ast = parse_query(f"\\where total {op} 3\n\\limit 2")
        assert ast.where.op == op


def test_where_values_are_unquoted_and_may_contain_spaces():
    assert parse_query('\\where status = "paid"\n\\limit 2').where.value == "paid"
    assert parse_query("\\where status = 'paid'\n\\limit 2").where.value == "paid"
    assert parse_query("\\where name = John Smith\n\\limit 2").where.value == "John Smith"
    assert parse_query('\\where status = "new"\n\\limit 2').where.value == "new"


def test_where_rejects_a_malformed_condition():
    ast = parse_query("\\where amount\n\\limit 2")
    assert messages(ast) == ["\\where expects `column op value`"]
    assert ast.where is None


def test_where_replaces_earlier_clauses():
    ast = parse_query("\\where a = 1\n\\where b = 2\n\\limit 2")
    assert (ast.where.column, ast.where.value, ast.where.line) == ("b", "2", 2)


# --- `\group` / `\order` / `\limit` ------------------------------------------


def test_group_splits_on_commas_and_whitespace():
    ast = parse_query("\\group user_id, region status\n\\limit 2")
    assert ast.errors == []
    assert [(g.line, g.column) for g in ast.group_by] == [(1, "user_id"), (1, "region"), (1, "status")]


def test_group_rejects_non_columns():
    ast = parse_query("\\group sum(amount) user_id\n\\limit 2")
    assert messages(ast) == ['\\group: not a column: "sum(amount)"']
    assert [g.column for g in ast.group_by] == ["user_id"]


def test_order_resolves_aliases_and_columns():
    ast = parse_query("\\from events\n\\select user_id as uid\n\\order uid desc\n\\limit 2")
    assert ast.errors == []
    assert (ast.order_by[0].target, ast.order_by[0].direction, ast.order_by[0].resolves_to) == (
        "uid",
        "desc",
        1,
    )

    ast = parse_query("\\from events\n\\select user_id\n\\order user_id\n\\limit 2")
    assert ast.order_by[0].resolves_to == 1


def test_order_resolves_derived_aggregate_and_rank_aliases():
    ast = parse_query("\\from events\n\\select sum(amount)\n\\order amount_sum\n\\limit 2")
    assert ast.errors == []
    assert ast.order_by[0].resolves_to == 1

    ast = parse_query("\\from events\n\\select sum(users.amount)\n\\order amount_sum\n\\limit 2")
    assert ast.order_by[0].resolves_to == 1

    ast = parse_query("\\from events\n\\select rank() over (order by ts)\n\\order rank\n\\limit 2")
    assert ast.order_by[0].resolves_to == 1

    ast = parse_query("\\from events\n\\select amount, sum(amount)\n\\order amount_sum\n\\limit 2")
    assert ast.order_by[0].resolves_to == 2

    # Alias-first: an explicit alias shadows a later item's derived name.
    ast = parse_query("\\from events\n\\select sum(amount) as total, amount_sum\n\\order amount_sum\n\\limit 2")
    assert ast.order_by[0].resolves_to == 2

    ast = parse_query("\\from events\n\\select amount_sum, sum(amount)\n\\order amount_sum\n\\limit 2")
    assert ast.order_by[0].resolves_to == 1


def test_order_targeting_a_case_alias_resolves_to_null():
    ast = parse_query(
        "\\from events\n"
        "\\select user_id\n"
        '\\case flag = when amount > 1 then "a" else "b"\n'
        "\\order flag\n"
        "\\limit 2"
    )
    assert ast.errors == []
    assert ast.order_by[0].resolves_to is None


def test_order_unknown_target_and_default_direction():
    ast = parse_query("\\from events\n\\order nope\n\\limit 2")
    assert ast.errors == []
    assert (ast.order_by[0].direction, ast.order_by[0].resolves_to) == ("asc", None)


def test_order_rejects_a_malformed_target():
    ast = parse_query("\\order amount ASC extra\n\\limit 2")
    assert messages(ast) == ["\\order expects `target [asc|desc]`"]
    assert ast.order_by == []


def test_limit_takes_the_last_non_negative_integer():
    assert parse_query("\\limit 0").limit == 0
    assert parse_query("\\limit 5\n\\limit 3").limit == 3
    assert parse_query("\\limit 12\n\\order user_id").limit == 12


def test_limit_rejects_non_integers():
    for bad in ("-1", "2.5", "abc", "1_000", "+3"):
        ast = parse_query(f"\\limit {bad}\n\\limit 1")
        assert messages(ast) == ["limit must be a non-negative integer"], bad
        assert ast.limit == 1


# --- command dispatch --------------------------------------------------------


def test_non_command_lines_and_unknown_commands():
    assert messages(parse_query("select amount\n\\limit 2")) == ["not a command — lines start with \\"]
    assert messages(parse_query("\\bogus 1\n\\limit 2")) == ['unknown command "\\bogus"']
    assert messages(parse_query("\\select\n\\limit 2")) == ["\\select expects arguments"]
    assert messages(parse_query("\\limit\n\\limit 2")) == ["\\limit expects arguments"]


def test_commands_are_case_insensitive():
    ast = parse_query("\\FROM events\n\\SELECT amount AS total\n\\LIMIT 4")
    assert ast.errors == []
    assert ast.from_.table == "events"
    assert ast.select[0].alias == "total"
    assert ast.limit == 4


def test_open_is_from():
    ast = parse_query("\\open events as e\n\\from users u\n\\limit 2")
    assert (ast.from_.line, ast.from_.table, ast.from_.alias) == (2, "users", "u")


# --- `\join` -----------------------------------------------------------------


def test_join_bare_shared_key_uses_the_left_side_for_both():
    ast = parse_query("\\from events\n\\join users on user_id\n\\limit 2")
    assert ast.errors == []
    join = ast.joins[0]
    assert (join.line, join.dataset, join.alias, join.left, join.right) == (2, "users", None, "user_id", "user_id")


def test_join_alias_forms():
    ast = parse_query("\\from events\n\\join users as u on user_id = id\n\\limit 2")
    assert (ast.joins[0].alias, ast.joins[0].left, ast.joins[0].right) == ("u", "user_id", "id")

    ast = parse_query("\\from events\n\\join users u on user_id = id\n\\limit 2")
    assert ast.joins[0].alias == "u"

    ast = parse_query("\\from events\n\\join users on user_id = id\n\\limit 2")
    assert ast.joins[0].alias is None


def test_join_keeps_document_order():
    ast = parse_query("\\from events\n\\join users on user_id\n\\join regions on region_id\n\\limit 2")
    assert [j.dataset for j in ast.joins] == ["users", "regions"]


def test_join_rejects_a_malformed_clause():
    ast = parse_query("\\from events\n\\join users\n\\limit 2")
    assert messages(ast) == ["\\join expects `<dataset> [as] <alias> on <col>[ = <col>]`"]
    assert ast.joins == []

    # The right-hand side is a bare column by grammar — only the left may be
    # identifier-qualified, so a qualified right side is a malformed clause.
    ast = parse_query("\\from events\n\\join users u on user_id = u.id\n\\limit 2")
    assert messages(ast) == ["\\join expects `<dataset> [as] <alias> on <col>[ = <col>]`"]
    assert ast.joins == []


def test_join_qualified_left_only_sees_identifiers_established_before_it(loaded):
    ast = parse_query("\\from events as e\n\\join users as u on e.user_id = id\n\\limit 2")
    assert ast.errors == []

    # The clause's own alias is not part of the accumulated left yet.
    ast = parse_query("\\from events as e\n\\join users as u on u.user_id = id\n\\limit 2")
    assert messages(ast) == ['unknown column "u.user_id" — "u" is not an open table: e']

    # A third clause sees both earlier identifiers.
    ast = parse_query(
        "\\from events as e\n"
        "\\join users as u on user_id = id\n"
        "\\join users as r on u.user_id = id\n"
        "\\limit 2"
    )
    assert ast.errors == []

    ast = parse_query(
        "\\from events as e\n"
        "\\join users as u on user_id = id\n"
        "\\join users as r on z.user_id = id\n"
        "\\limit 2"
    )
    assert messages(ast) == ['unknown column "z.user_id" — "z" is not an open table: e, u']


# --- helpers -----------------------------------------------------------------


def test_split_top_splits_only_at_paren_depth_zero():
    assert split_top("a, sum(b) as x, c") == ["a", "sum(b) as x", "c"]
    assert split_top("count(a, b), c") == ["count(a, b)", "c"]
    assert split_top("sum(f(a, b))") == ["sum(f(a, b))"]
    assert split_top(",a,") == ["a"]
    assert split_top("   ") == []
    assert split_top("") == []
    assert split_top("a") == ["a"]


def test_split_top_drives_multi_expression_select_lines():
    ast = parse_query("\\from events\n\\select user_id, sum(amount) as total\n\\limit 2")
    assert ast.errors == []
    assert [(s.raw, s.alias) for s in ast.select] == [("user_id", None), ("sum(amount) as total", "total")]


def test_unquote_strips_one_layer_of_matching_quotes():
    assert unquote('"a b"') == "a b"
    assert unquote("'a b'") == "a b"
    assert unquote("  \"a b\"  ") == "a b"
    assert unquote("a b") == "a b"
    assert unquote('""') == ""
    assert unquote('" "') == " "
    assert unquote('"a\'') == '"a\''
    assert unquote("'a\"") == "'a\""
    assert unquote('""a""') == '"a"'


def test_auto_alias_derivation():
    assert auto_alias("sum", "amount") == "amount_sum"
    assert auto_alias("SUM", "AMOUNT") == "amount_sum"
    assert auto_alias("sum", "users.amount") == "amount_sum"
    assert auto_alias("count", "user_id") == "user_id_count"
    assert auto_alias("year", "ts") == "ts_year"
    assert auto_alias("sum", "a-b") == "a_b_sum"


# --- AST -> engine payload ---------------------------------------------------


def test_payload_maps_the_canonical_document():
    ast = parse_query(CANONICAL_DOC)
    payload = payload_from_ast(ast)
    assert payload["dataset"] == "recent"
    assert payload["alias"] is None
    assert payload["joins"] == []
    assert payload["where"] is None
    assert payload["groupBy"] == []
    assert payload["limit"] == 5
    assert payload["orderBy"] == [{"target": "customer_total", "direction": "desc"}]
    assert payload["cases"] == [
        {
            "alias": "flag",
            "whens": [{"column": "amount", "op": ">", "value": "100", "then": "high"}],
            "else": "low",
        }
    ]

    customer, month_col, total, rank_col = payload["select"]
    assert customer["column"] == "customer_id"
    assert customer["alias"] is None
    assert month_col["temporal"] == {"fn": "month", "arg": "placed_at"}
    assert month_col["alias"] == "placed_at_month"
    assert total == {
        "column": None,
        "star": False,
        "aggregate": {"fn": "sum", "arg": "amount"},
        "temporal": None,
        "rank": None,
        "window": {"partitionBy": ["customer_id"], "order": None},
        "alias": "customer_total",
    }
    assert rank_col == {
        "column": None,
        "star": False,
        "aggregate": None,
        "temporal": None,
        "rank": {"fn": "rank"},
        "window": {"partitionBy": [], "order": {"column": "placed_at", "direction": "desc"}},
        "alias": "recency_rank",
    }

    # A CTE body is a full payload whose own ctes list is empty (by grammar).
    assert payload["ctes"] == [
        {
            "name": "recent",
            "body": {
                "dataset": "orders",
                "alias": None,
                "joins": [],
                "select": [],
                "where": {"column": "status", "op": "=", "value": "paid"},
                "groupBy": [],
                "orderBy": [],
                "limit": None,
                "cases": [],
                "ctes": [],
            },
        }
    ]


def test_payload_sends_derived_aliases_for_unaliased_aggregates():
    ast = parse_query("\\from events\n\\select sum(amount), count(user_id), year(ts)\n\\limit 2")
    assert [s["alias"] for s in payload_from_ast(ast)["select"]] == ["amount_sum", "user_id_count", "ts_year"]

    ast = parse_query("\\from events\n\\select sum(users.amount) as revenue\n\\limit 2")
    assert payload_from_ast(ast)["select"][0]["alias"] == "revenue"

    ast = parse_query("\\from events\n\\select sum(users.amount)\n\\limit 2")
    assert payload_from_ast(ast)["select"][0]["alias"] == "amount_sum"


def test_payload_maps_from_joins_where_group_order_and_limit():
    ast = parse_query(
        "\\from events as e\n"
        "\\join users u on user_id = id\n"
        "\\where amount > 10\n"
        "\\group user_id region\n"
        "\\order user_id desc\n"
        "\\limit 7\n"
        "\\select user_id"
    )
    assert ast.errors == []
    payload = payload_from_ast(ast)
    assert payload["dataset"] == "events"
    assert payload["alias"] == "e"
    assert payload["joins"] == [{"dataset": "users", "alias": "u", "left": "user_id", "right": "id"}]
    assert payload["where"] == {"column": "amount", "op": ">", "value": "10"}
    assert payload["groupBy"] == ["user_id", "region"]
    assert payload["orderBy"] == [{"target": "user_id", "direction": "desc"}]
    assert payload["limit"] == 7


def test_payload_without_from_is_empty_dataset():
    payload = payload_from_ast(parse_query("\\select amount"))
    assert payload["dataset"] == ""
    assert payload["alias"] is None
    assert payload["limit"] is None
    assert payload["select"][0]["column"] == "amount"


def test_payload_nests_cte_bodies_recursively():
    ast = parse_query(
        "\\with first\n"
        "  \\from events\n"
        "  \\select sum(amount)\n"
        "\\with second\n"
        "  \\from first\n"
        "  \\select amount_sum\n"
        "\\from second\n"
        "\\select amount_sum"
    )
    assert ast.errors == []
    payload = payload_from_ast(ast)
    assert [c["name"] for c in payload["ctes"]] == ["first", "second"]
    first, second = payload["ctes"]
    assert first["body"]["dataset"] == "events"
    assert first["body"]["select"][0]["alias"] == "amount_sum"
    assert second["body"]["dataset"] == "first"
    assert second["body"]["select"][0]["column"] == "amount_sum"
    assert second["body"]["ctes"] == []
