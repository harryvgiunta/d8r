"""Throwaway JSON execution proof on the authorized loopback PostgreSQL service."""
import json
from pathlib import Path

import ibis
import ibis.expr.datatypes as dt
import psycopg

from d8r.engine import expression, tx
from d8r.engine.execute import execute
from d8r.query import parse_query, payload_from_ast


def payload(document):
    ast = parse_query(document, settled=True)
    assert not ast.errors
    return payload_from_ast(ast)


password = next(line.split("=", 1)[1].strip().strip("\"'")
                for line in Path(".env.postgres").read_text().splitlines()
                if line.startswith("D8R_POSTGRES_PASSWORD="))
raw = psycopg.connect(host="127.0.0.1", port=55433, dbname="d8r", user="d8r", password=password,
                      autocommit=True, connect_timeout=10)
con = ibis.postgres.from_connection(raw)
try:
    raw.execute("CREATE TEMPORARY TABLE d8r_json_smoke (id bigint, details_json text, native_json json, native_jsonb jsonb, lookup text, idx bigint)")
    document = {"customer": {"name": 'a "quoted" dotted.name', "active": True},
                "items": [{"amount_cents": 199, "discount": 0.5}], "flag": False,
                'quote"dot.key': "quoted", "nil": None}
    encoded = json.dumps(document)
    with raw.cursor() as cursor:
        cursor.executemany("INSERT INTO pg_temp.d8r_json_smoke VALUES (%s, %s, %s, %s, %s, %s)",
                           [(1, encoded, encoded, encoded, 'quote"dot.key', 0),
                            (2, "{}", "{}", "{}", "missing", -1),
                            (3, None, None, None, None, None)])
    schema = ibis.schema({"id": "int64", "details_json": "string", "native_json": "json",
                          "native_jsonb": dt.JSON(binary=True), "lookup": "string", "idx": "int64"})
    handle = tx.temp_handle(con, "d8r_json_smoke", schema)
    tables = {"json_records": handle}
    proofs = []
    for field in ("details_json", "native_json", "native_jsonb"):
        query = ("\\from json_records\n\\select id, "
                 f"json_text({field}, 'customer', 'name') as name, "
                 f"json_int({field}, 'items', 0, 'amount_cents') as cents, "
                 f"json_float({field}, 'items', 0, 'discount') as discount, "
                 f"json_bool({field}, 'customer', 'active') as active, "
                 f"json_bool({field}, 'flag') as flag, "
                 f"json_text({field}, lookup) as quoted, "
                 f"json_int({field}, 'items', idx, 'amount_cents') as indexed\n\\order id")
        result = execute(con, payload(query), "postgres", tables)
        assert result["rows"][0] == [1, 'a "quoted" dotted.name', 199, 0.5, True, False, "quoted", 199], result["rows"]
        assert all(value is None for value in result["rows"][1][1:])
        assert all(value is None for value in result["rows"][2][1:])
        proofs.append(field)
    query = ("\\with extracted\n  \\from json_records\n"
             "  \\select json_bool(native_jsonb, 'customer', 'active') as active, "
             "json_int(native_jsonb, 'items', 0, 'amount_cents') as cents\n"
             "\\from extracted\n\\where active = true\n\\select sum(cents) / 100 as dollars")
    assert execute(con, payload(query), "postgres", tables)["rows"] == [[1.99]]
    print(json.dumps({"postgresql": "actual loopback service", "storage_types_executed": proofs,
                      "quoted_and_dynamic_keys": True, "nested_array_indices": True,
                      "boolean_filter_and_numeric_aggregate": True, "persistent_tables_modified": False}))
finally:
    con.disconnect()
