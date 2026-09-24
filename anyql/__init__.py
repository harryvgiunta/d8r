"""anyQL — pure-Python port of the anyQL query language layer (and engine).

The language layer lives under `anyql.query`: `parse_query` turns command text
into a `QueryAST`, `payload_from_ast` turns that AST into the payload the
engine consumes, and `anyql.query.schema` is the schema-registry seam the
parser's validation reads. Nothing here imports the engine or any UI code.
"""
