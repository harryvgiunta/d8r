"""Derived relation completion is typed, lexically scoped, and entirely offline."""

from dataclasses import replace

import pytest

from d8r.query import ColumnDef, FnDef, SchemaContext, TableDef
from d8r.tui.palette import view_for
from d8r.tui.session import Session


@pytest.fixture
def completion_session(sources, tmp_path, monkeypatch):
    session = Session(sources, data_dir=tmp_path)
    session.schema = SchemaContext(tables=(
        TableDef("events", columns=(
            ColumnDef("id", "int64"),
            ColumnDef("amount", "float64"),
            ColumnDef("kind", "string"),
            ColumnDef("occurred_at", "timestamp"),
        )),
        TableDef("users", columns=(ColumnDef("id", "int64"), ColumnDef("name", "string"))),
    ))

    def forbidden(*args, **kwargs):
        pytest.fail("completion must not access a connection or execute a query")

    for source in session.sources.values():
        for method in ("table", "execute", "raw_sql"):
            if hasattr(source.con, method):
                monkeypatch.setattr(source.con, method, forbidden)
    monkeypatch.setattr("ibis.expr.types.Table.execute", forbidden)
    return session


def fields(session, doc, **kwargs):
    return [(name, dtype) for name, dtype, _ in session.column_entries(doc, **kwargs)]


def test_implicit_cte_exports_engine_projection_names_and_types(completion_session):
    doc = r"""\with totals
  \from events
  \select kind as category, sum(amount) as total, count(*) as rows, avg(amount)
\select """
    assert fields(completion_session, doc) == [
        ("category", "string"), ("total", "float64"), ("rows", "int64"), ("amount_avg", "float64"),
    ]


@pytest.mark.parametrize("clause", ["select", "where", "group", "order"])
def test_all_column_clauses_offer_implicit_cte_fields(completion_session, clause):
    line = "\\" + clause + " cat"
    doc = "\\with labels\n  \\from events\n  \\select kind as category\n" + line
    view = view_for(completion_session, doc, line, len(line), row=3)
    assert view is not None
    category = next(entry for entry in view.entries if entry.label == "category")
    assert category.insert == "category "
    assert category.detail == "string · labels"


def test_passthrough_star_and_projected_aliases_keep_engine_output_order(completion_session):
    doc = r"""\with first
  \from events
  \select id as code, *
\with copied
  \from first
\with starred
  \from copied
  \select *
\select """
    assert fields(completion_session, doc) == [
        ("code", "int64"), ("id", "int64"), ("amount", "float64"),
        ("kind", "string"), ("occurred_at", "timestamp"),
    ]


def test_saved_functions_and_their_local_ctes_export_all_output_fields(completion_session):
    session = completion_session
    session.schema = replace(session.schema, fns=(
        FnDef("leaf", ("tag",), r"""\with chosen
  \from events
  \select id as event_id, kind, @tag as tag
\with renamed
  \from chosen
  \select event_id, upper(kind) as label, tag"""),
        FnDef("wrapped", ("tag",), r"""\with chosen
  \leaf(@tag)
\with copied
  \from chosen
  \select *"""),
    ))
    doc = r"""\with chosen
  \from users
  \select name as caller_only
\with result
  \wrapped('example')
\select """
    assert fields(session, doc) == [("event_id", "int64"), ("label", "string"), ("tag", "string")]
    assert fields(session, "\\wrapped('example') as f\n\\select ") == [
        ("f.event_id", "int64"), ("f.label", "string"), ("f.tag", "string"),
    ]


def test_aliases_and_joined_derived_sources_keep_source_order(completion_session):
    doc = r"""\with selected
  \from events
  \select id as event_id, kind as category
\from selected s
\join (\from users \select id as user_id, name as title) u on s.event_id = u.user_id
\select """
    assert fields(completion_session, doc) == [
        ("s.event_id", "int64"), ("s.category", "string"), ("u.user_id", "int64"), ("u.title", "string"),
    ]
    line = "\\select s.cat"
    view = view_for(completion_session, doc + "s.cat", line, len(line))
    assert view is not None
    assert view.labels == ["s.category"]


def test_cte_body_keeps_preceding_siblings_but_not_later_or_other_statements(completion_session):
    doc = r"""\with old
  \from users
  \select name as old_label
;
\with first
  \from events
  \select kind as category
\with second
  \from first f
  \select f.cat
\with later
  \from users
  \select name as future_label
\from later"""
    line = "  \\select f.cat"
    view = view_for(completion_session, doc, line, len(line), row=9)
    assert view is not None
    assert view.labels == ["f.category"]
    dataset_line = "  \\from "
    edited = doc.replace("  \\from first f", dataset_line)
    view = view_for(completion_session, edited, dataset_line, len(dataset_line), row=8)
    assert view is not None
    assert "first" in view.labels
    assert not {"second", "later", "old"}.intersection(view.labels)


@pytest.mark.parametrize("partial", [False, True])
def test_cte_body_context_does_not_let_functions_capture_caller_ctes(completion_session, partial):
    session = completion_session
    session.schema = replace(session.schema, tables_complete=not partial, fns=(
        FnDef("capture", body="\\from caller_only\n\\select secret"),
    ))
    doc = r"""\with caller_only
  \from users
  \select name as secret
\with consuming
  \capture()
  \select sec"""
    line = "  \\select sec"
    view = view_for(session, doc, line, len(line), row=5)
    assert view is None


def test_incomplete_outer_clauses_do_not_hide_valid_source_fields(completion_session):
    doc = r"""\with labeled
  \from events
  \select kind as category, amount as total
\where category =
\select sum("""
    assert fields(completion_session, doc) == [("category", "string"), ("total", "float64")]
    line = "\\select sum("
    view = view_for(completion_session, doc, line, len(line), row=4)
    assert view is not None
    assert "total" in view.labels
    assert "amount" not in view.labels


def test_unindexed_or_incomplete_cte_never_offers_unrelated_global_fields(completion_session):
    session = completion_session
    session.schema = replace(session.schema, tables_complete=False)
    assert fields(session, "\\with pending\n  \\from unindexed\n\\select ") == []
    assert fields(session, "\\with pending\n  \\from\n\\select ") == []


def test_offline_inference_respects_default_schema_and_captured_snapshot(completion_session):
    session = completion_session
    snapshot = SchemaContext(tables=(
        TableDef("public.records", columns=(ColumnDef("public_id", "int64"),)),
        TableDef("etl.records", columns=(ColumnDef("selected_id", "string"),)),
    ), default_schema="etl")
    doc = "\\with selected\n  \\from records\n\\select "
    assert fields(session, doc, schema=snapshot) == [("selected_id", "string")]
    assert fields(session, doc) == []


def test_physical_sources_keep_existing_qualification_and_pool_behavior(completion_session):
    assert fields(completion_session, "\\from users u\n\\select ") == [
        ("u.id", "int64"), ("u.name", "string"),
    ]
    assert fields(completion_session, "\\from users\n\\select ", qualified=True) == [
        ("users.id", "int64"), ("users.name", "string"),
    ]
    assert fields(completion_session, "\\select ") == [
        ("id", "int64"), ("amount", "float64"), ("kind", "string"),
        ("occurred_at", "timestamp"), ("name", "string"),
    ]


def test_lateral_subquery_and_window_results_retain_real_types(completion_session):
    doc = r"""\from users u
\join lateral (\from events \where id = u.id \select id, amount / 2 as half, row_number() over (order by amount) as position) e
\select """
    assert fields(completion_session, doc) == [
        ("u.id", "int64"), ("u.name", "string"),
        ("e.id", "int64"), ("e.half", "float64"), ("e.position", "int64"),
    ]


def test_function_capture_inside_cte_join_cannot_become_a_valid_schema(completion_session):
    session = completion_session
    session.schema = replace(session.schema, tables_complete=False, fns=(
        FnDef("capture", body="\\from caller_only"),
    ))
    doc = r"""\with caller_only
  \from users
\with consuming
  \from events e
  \join capture() c on e.id = c.id
  \select c.name as stolen
\from consuming
\select """
    assert fields(session, doc) == []


def test_cross_sources_offer_ctes_and_saved_calls_offline(completion_session):
    session = completion_session
    session.schema = replace(session.schema, fns=(
        FnDef("labels", body="\\from users\n\\select name as title"),
    ))
    prefix = "\\with chosen\n  \\from users\n  \\select name as title\n\\from events c\n"
    line = "\\join cross "
    view = view_for(session, prefix + line, line, len(line), row=4)
    assert set(view.labels) == {"chosen", "events", "users", "labels()"}
    chosen = next(entry for entry in view.entries if entry.label == "chosen")
    assert chosen.insert == "chosen c2 "
    call = next(entry for entry in view.entries if entry.label == "labels()")
    assert call.insert == "labels()" and call.cursor_back == 1
    for source in ("chosen", "labels()", "(\\from users \\select name as title)"):
        line = "\\select x.tit"
        doc = prefix + "\\join cross " + source + " as x\n" + line
        view = view_for(session, doc, line, len(line), row=5)
        assert view.labels == ["x.title"]
        assert view.entries[0].detail.startswith("string")
