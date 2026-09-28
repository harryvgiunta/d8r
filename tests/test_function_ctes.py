"""Function-local CTE chains execute with lexical scope and bounded expansion."""
from __future__ import annotations

import datetime as dt

import ibis
import pytest

from d8r.engine import DataSource
from d8r.engine.execute import execute
from d8r.query import FnDef, SchemaContext, parse_query, payload_from_ast
from d8r.query.parser import MAX_FUNCTION_DEPTH, MAX_FUNCTION_EXPANSIONS
from d8r.tui.session import Session


CLEAN_PROJECTS = r"""\with eligible_free
  \from projects
  \where plan_id = 'free'
  \select id as project_id
\with banned
  \from project_bans
  \where status = 'active'
  \select project_id as project_id
\with not_banned
  \from eligible_free
  \except banned
\with sub_history
  \from billing_subscription_packs
  \select project_id as project_id
\with pay_history
  \from billing_events
  \where status = 'success'
  \select project_id as project_id
\with ever_paid_or_subscribed
  \from sub_history
  \select project_id
  \union all pay_history
\with clean
  \from not_banned
  \except ever_paid_or_subscribed
\from clean c
\join projects p on c.project_id = p.id
\select p.id, p.name, p.primary_email, p.created_at, p.plan_id
\order created_at desc
\limit 25"""

TAGGED_PROJECT = r"""\with chosen
  \from projects
  \where id = @id
  \select id, @tag as tag
\with labeled
  \from chosen c
  \select c.id, c.tag
\from labeled l
\select l.id, l.tag"""


@pytest.fixture
def project_session(tmp_path):
    con = ibis.duckdb.connect()
    source = DataSource(
        id="demo", display="Synthetic projects", doc="", kind="demo",
        dialect="duckdb", dir=tmp_path, con=con,
    )
    data = {
        "projects": {
            "id": list(range(1, 36)),
            "name": [f"project-{i}" for i in range(1, 36)],
            "primary_email": [f"p{i}@example.test" for i in range(1, 36)],
            "created_at": [dt.datetime(2026, 1, 1) + dt.timedelta(days=i) for i in range(1, 36)],
            "plan_id": ["paid" if i == 31 else "free" for i in range(1, 36)],
        },
        "project_bans": {"project_id": [34, 33], "status": ["active", "inactive"]},
        "billing_subscription_packs": {"project_id": [32, 30]},
        "billing_events": {"project_id": [30, 29], "status": ["success", "failed"]},
    }
    try:
        for name, columns in data.items():
            table = con.create_table(name, ibis.memtable(columns))
            source.datasets[name] = {"table": table, "doc": "", "rows": len(next(iter(columns.values())))}
        session = Session({source.id: source}, data_dir=tmp_path / "state")
        yield session
    finally:
        con.disconnect()


def test_save_preview_and_engine_execute_full_cte_chain(project_session):
    session = project_session
    session.save_fn("clean_projects", "", CLEAN_PROJECTS, "Never-paid free projects")
    expected_ids = [35, 33, *range(29, 6, -1)]
    expected = [
        [i, f"project-{i}", f"p{i}@example.test",
         (dt.datetime(2026, 1, 1) + dt.timedelta(days=i)).isoformat(), "free"]
        for i in expected_ids
    ]
    preview = session.fn_preview("clean_projects")
    assert preview.error == ""
    assert preview.columns == ["id", "name", "primary_email", "created_at", "plan_id"]
    assert preview.rows == expected
    assert session.history == []

    ast = parse_query("\\from clean_projects()", schema=session.schema, settled=True)
    assert ast.errors == []
    result = execute(session.source.con, payload_from_ast(ast), tables=session.tables())
    assert result["rows"] == expected


def test_local_names_do_not_collide_or_leak_across_nested_and_sibling_calls(project_session):
    session = project_session
    session.save_fn("tagged", "id, tag", TAGGED_PROJECT, "")
    session.save_fn("wrapped", "id, tag", r"""\with chosen
  \from tagged(@id, @tag)
\from chosen""", "")
    outcome = session.run(r"""\with chosen
  \from projects
  \where id = 35
  \select id, 'caller' as tag
\with invocations
  \from tagged(1, '(first)')
  \union all tagged(2, "O'Reilly")
  \union all tagged(1, '(first)')
  \union all wrapped(3, 'nested')
\from invocations
\union all chosen
\order id""")
    assert outcome.error == ""
    assert outcome.rows == [[1, "(first)"], [1, "(first)"], [2, "O'Reilly"], [3, "nested"], [35, "caller"]]
    assert 'unknown table "chosen"' in session.run("\\from chosen").error


def test_local_ctes_are_visible_in_inline_relations_and_operands(project_session):
    session = project_session
    session.save_fn("inline_ctes", "cap", r"""\with chosen
  \from projects
  \where id <= @cap
  \select id
\with mirrored
  \from (\from chosen \select id) seed
\from (\from mirrored \select id) r
\where id in (\from chosen \select id)
\select id
\union all (\from chosen \where id = 1 \select id)
\order id""", "")
    outcome = session.fn_preview("inline_ctes", "2")
    assert outcome.error == ""
    assert outcome.rows == [[1], [1], [2]]


def test_function_does_not_capture_a_callers_cte(project_session):
    session = project_session
    schema = SchemaContext(tables=session.schema.tables, fns=[FnDef("capture", body=r"""\with own
  \from caller_only
\from own""")])
    ast = parse_query(r"""\with caller_only
  \from projects
\from capture()""", schema=schema, settled=True)
    assert any('unknown table "caller_only"' in error.message for error in ast.errors)
    assert {error.line for error in ast.errors} == {3}


@pytest.mark.parametrize(("body", "error"), [
    (r"""\with first
  \from later
\with later
  \from projects
\from first""", 'unknown table "later"'),
    (r"""\with chosen
  \from projects
\with chosen
  \from projects
\from chosen""", "duplicate CTE name"),
    (r"""\with chosen
  \with nested
    \from projects
  \from nested
\from chosen""", "nested CTEs are not supported"),
    (r"""\with projects
  \from project_bans
\from projects""", "shadows dataset"),
    (r"""\with chosen
  \from projects
\from chosen
\temp forbidden""", "only allowed in the document"),
    (r"""\with chosen
  \from projects
\from chosen
\begin""", "only allowed in the document"),
])
def test_function_save_preserves_cte_and_statement_boundaries(project_session, body, error):
    with pytest.raises(ValueError, match=error):
        project_session.save_fn("invalid", "", body, "")
    assert "invalid" not in project_session.fns


def test_recursion_through_local_ctes_cannot_replace_a_saved_function(project_session):
    session = project_session
    body = "\\with chosen\n  \\from projects\n  \\where id = 1\n\\from chosen"
    session.save_fn("leaf", "", body, "")
    session.save_fn("wrapper", "", "\\with chosen\n  \\from leaf()\n\\from chosen", "")
    with pytest.raises(ValueError, match="recursive function call"):
        session.save_fn("leaf", "", "\\with chosen\n  \\from wrapper()\n\\from chosen", "")
    outcome = session.fn_preview("wrapper")
    assert outcome.error == ""
    assert [row[0] for row in outcome.rows] == [1]


def test_function_depth_limit_survives_local_cte_scopes(project_session):
    functions = [FnDef("leaf", body="\\with chosen\n  \\from projects\n  \\where id = 1\n\\from chosen\n\\select id")]
    for index in range(MAX_FUNCTION_DEPTH):
        target = "leaf" if index == 0 else f"chain{index - 1}"
        functions.append(FnDef(f"chain{index}", body=f"\\with chosen\n  \\from {target}()\n\\from chosen"))
    schema = SchemaContext(tables=project_session.schema.tables, fns=functions)
    allowed = parse_query(f"\\from chain{MAX_FUNCTION_DEPTH - 2}()", schema=schema, settled=True)
    assert allowed.errors == []
    result = execute(project_session.source.con, payload_from_ast(allowed), tables=project_session.tables())
    assert result["rows"] == [[1]]
    refused = parse_query(f"\\from chain{MAX_FUNCTION_DEPTH - 1}()", schema=schema, settled=True)
    assert any("nested calls" in error.message for error in refused.errors)


def test_function_expansion_budget_is_shared_across_local_ctes():
    leaf = FnDef("leaf", body="\\with chosen\n  \\from projects\n\\from chosen")
    body = (
        "\\with left_calls\n  \\from leaf()"
        + "\n  \\union all leaf()" * (MAX_FUNCTION_EXPANSIONS // 2 - 2)
        + "\n\\with right_calls\n  \\from leaf()"
        + "\n  \\union all leaf()" * (MAX_FUNCTION_EXPANSIONS // 2 - 1)
        + "\n\\from left_calls\n\\union all right_calls"
    )
    schema = SchemaContext(fns=[leaf, FnDef("branches", body=body)])
    assert parse_query("\\from branches()", schema=schema, settled=True).errors == []
    over_budget = SchemaContext(fns=[leaf, FnDef("branches", body=body + "\n\\union all leaf()")])
    refused = parse_query("\\from branches()", schema=over_budget, settled=True)
    assert any("function expansions" in error.message for error in refused.errors)
