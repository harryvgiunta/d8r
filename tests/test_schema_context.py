"""Schema snapshots isolate parsing, completion, and saved-function expansion."""

from dataclasses import FrozenInstanceError

import pytest

from d8r.engine.execute import execute
from d8r.query import Capabilities, ColumnDef, FnDef, ForeignKey, SchemaContext, TableDef, parse_query, payload_from_ast
from d8r.tui.palette import view_for
from d8r.tui.session import Session


def test_sessions_keep_their_own_datasets_and_function_signatures(sources, tmp_path):
    demo = Session(sources, data_dir=tmp_path / "demo-memory")
    mysql = Session(sources, data_dir=tmp_path / "mysql-memory")
    mysql.set_active("mysql")
    demo.save_fn("sample", "marker", "\\from events\n\\select @marker as marker\n\\limit 1", "")
    mysql.save_fn("sample", "", "\\from products\n\\select 'mysql' as marker\n\\limit 1", "")

    demo_result = demo.run("\\from sample('demo')\n\\select *")
    mysql_result = mysql.run("\\from sample()\n\\select *")
    assert demo_result.error == mysql_result.error == ""
    assert demo_result.rows == [["demo"]]
    assert mysql_result.rows == [["mysql"]]
    assert demo.run("\\from sample()\n\\select *").error
    assert mysql.run("\\from sample('demo')\n\\select *").error

    line = "\\from "
    demo_choices = view_for(demo, line, line, len(line)).labels
    mysql_choices = view_for(mysql, line, line, len(line)).labels
    assert "events" in demo_choices and "products" not in demo_choices
    assert "products" in mysql_choices and "events" not in mysql_choices
    assert parse_query("\\from products\n\\select *", schema=demo.schema).errors
    assert parse_query("\\from events\n\\select *", schema=mysql.schema).errors
    assert parse_query("\\from unknown_dataset\n\\select *").errors == []


def test_captured_context_survives_function_replacement_deletion_and_source_switch(sources, tmp_path):
    session = Session(sources, data_dir=tmp_path / "memory")
    session.save_fn("sample", "", "\\from events\n\\select 'before' as marker\n\\limit 1", "")
    original = session.schema
    session.save_fn("sample", "", "\\from events\n\\select 'after' as marker\n\\limit 1", "")
    replacement = session.schema
    session.set_active("mysql")
    session.delete_fn("sample")

    document = "\\from sample()\n\\select *"
    for context, marker in ((original, "before"), (replacement, "after")):
        ast = parse_query(document, schema=context)
        assert ast.errors == []
        result = execute(sources["demo"].con, payload_from_ast(ast))
        assert result["rows"] == [[marker]]
        assert parse_query("\\from products\n\\select *", schema=context).errors
    assert parse_query(document, schema=session.schema).errors
    assert parse_query("\\from products\n\\select *", schema=session.schema).errors == []


def test_constructor_inputs_cannot_mutate_a_schema_snapshot():
    values = ["active"]
    columns = [ColumnDef("status", "string", values=values)]
    key_columns = ["status"]
    target_columns = ["code"]
    foreign_keys = [ForeignKey(key_columns, "statuses", target_columns)]
    tables = [TableDef("events", columns=columns, foreign_keys=foreign_keys)]
    params = ["value"]
    functions = [FnDef("sample", params, "\\from events\n\\select @value as marker")]
    aggregates = ["count"]
    scalar_names = ["upper"]
    scalar_functions = {"string": scalar_names}
    operators = ["="]
    windows = ["row_number"]
    supports = {"regex": True}
    capabilities = Capabilities("snapshot", aggregates, scalar_functions, operators, windows, supports)
    context = SchemaContext(tables, capabilities, functions)

    values.append("injected")
    columns.clear()
    key_columns.clear()
    target_columns.clear()
    foreign_keys.clear()
    tables.clear()
    params[0] = "undeclared"
    functions.clear()
    aggregates.clear()
    scalar_names.append("injected")
    scalar_functions.clear()
    operators.clear()
    windows.clear()
    supports["regex"] = False

    ast = parse_query("\\from sample('kept')\n\\select *", schema=context)
    assert ast.errors == []
    assert ast.from_.body.select[0].literal.value == "kept"
    assert context.table_by_name("events").columns[0].values == ("active",)
    assert context.table_by_name("events").foreign_keys == (ForeignKey(("status",), "statuses", ("code",)),)
    assert context.column_by_name("status").values == ("active",)
    assert context.capabilities.aggregates == ("count",)
    assert context.capabilities.functions["string"] == ("upper",)
    assert context.capabilities.operators == ("=",)
    assert context.capabilities.window_functions == ("row_number",)
    assert context.capabilities.supports["regex"] is True


def test_snapshot_records_and_nested_containers_reject_mutation():
    context = SchemaContext(
        [TableDef("events", columns=[ColumnDef("status", "string", values=["active"])],
                  foreign_keys=[ForeignKey(["status"], "statuses", ["code"])])],
        Capabilities("snapshot", ["count"], {"string": ["upper"]}, ["="], supports={"regex": True}),
        [FnDef("sample", ["value"], "\\from events\n\\select @value as marker")],
    )
    with pytest.raises(FrozenInstanceError):
        context.tables = ()
    with pytest.raises(FrozenInstanceError):
        context.tables[0].name = "renamed"
    with pytest.raises(FrozenInstanceError):
        context.tables[0].columns[0].type = "int64"
    with pytest.raises(FrozenInstanceError):
        context.tables[0].foreign_keys[0].target_table = "injected"
    with pytest.raises(TypeError):
        context.tables[0].foreign_keys[0].columns[0] = "injected"
    with pytest.raises(TypeError):
        context.tables[0].foreign_keys[0].target_columns[0] = "injected"
    with pytest.raises(TypeError):
        context.tables[0].foreign_keys[0] = ForeignKey((), "injected", ())
    with pytest.raises(FrozenInstanceError):
        context.fns[0].body = "\\from missing"
    with pytest.raises(FrozenInstanceError):
        context.pool[0].tables = ("missing",)
    with pytest.raises(TypeError):
        context.tables[0].columns[0].values[0] = "injected"
    with pytest.raises(TypeError):
        context.fns[0].params[0] = "injected"
    with pytest.raises(TypeError):
        context.capabilities.functions["string"][0] = "injected"
    with pytest.raises(TypeError):
        context.capabilities.functions["string"] = ("injected",)
    with pytest.raises(TypeError):
        context.capabilities.supports["regex"] = False


@pytest.mark.parametrize("document", [
    "\\from unindexed\n\\select id",
    "\\from events\n\\join unindexed on id = id\n\\select events.id",
    "\\from events\n\\union unindexed\n\\select id",
    "\\with recent\n  \\from unindexed\n  \\select id\n\\from recent\n\\select id",
    "\\from ( \\from unindexed \\select id ) as nested\n\\select id",
])
def test_partial_index_does_not_reject_unindexed_relations(document):
    known = (TableDef("events", columns=(ColumnDef("id", "int64"),)),)
    partial = SchemaContext(tables=known, tables_complete=False)
    complete = SchemaContext(tables=known)
    assert parse_query(document, schema=partial, settled=True).errors == []
    assert any('unknown table "unindexed"' in error.message
               for error in parse_query(document, schema=complete, settled=True).errors)
    assert partial.column_by_name("id").tables == ("events",)
