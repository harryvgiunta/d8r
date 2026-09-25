"""D8R — pure-Python port of the D8R query language layer (and engine).

The language layer lives under `d8r.query`: `parse_query` turns command text
into a `QueryAST`, `payload_from_ast` turns that AST into the payload the
engine consumes, and `d8r.query.schema` is the schema-registry seam the
parser's validation reads. Nothing here imports the engine or any UI code.
"""
