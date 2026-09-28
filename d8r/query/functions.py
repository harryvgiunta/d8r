"""Scalar call signatures shared by parsing, Ibis compilation, and completion."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ArgumentKind = Literal["string", "integer", "any"]
ResultKind = Literal["string", "integer", "boolean", "date", "time", "timestamp", "any"]

ARITHMETIC_PRECEDENCE = {"+": 1, "-": 1, "*": 2, "/": 2}
MAX_EXPRESSION_DEPTH = 32
MAX_EXPRESSION_NODES = 256
MAX_WHERE_DEPTH = 32
MAX_WHERE_NODES = 256


@dataclass(frozen=True)
class ScalarFunction:
    parameters: tuple[ArgumentKind, ...]
    minimum: int
    result: ResultKind = "string"
    variadic: bool = False

    def accepts(self, count: int) -> bool:
        return count >= self.minimum and (self.variadic or count <= len(self.parameters))

    def argument_kind(self, index: int) -> ArgumentKind | None:
        if index < len(self.parameters):
            return self.parameters[index]
        return self.parameters[-1] if self.variadic else None


SCALAR_FUNCTIONS: dict[str, ScalarFunction] = {
    "string": ScalarFunction(("any",), 1),
    "coalesce": ScalarFunction(("any", "any"), 2, "any", variadic=True),
    "nullif": ScalarFunction(("any", "any"), 2, "any"),
    "concat": ScalarFunction(("string", "string"), 2, variadic=True),
    "concat_ws": ScalarFunction(("string", "string"), 2, variadic=True),
    "lower": ScalarFunction(("string",), 1),
    "upper": ScalarFunction(("string",), 1),
    "capitalize": ScalarFunction(("string",), 1),
    "length": ScalarFunction(("string",), 1, "integer"),
    "strip": ScalarFunction(("string",), 1),
    "lstrip": ScalarFunction(("string",), 1),
    "rstrip": ScalarFunction(("string",), 1),
    "substr": ScalarFunction(("string", "integer", "integer"), 2),
    "left": ScalarFunction(("string", "integer"), 2),
    "right": ScalarFunction(("string", "integer"), 2),
    "replace": ScalarFunction(("string", "string", "string"), 3),
    "contains": ScalarFunction(("string", "string"), 2, "boolean"),
    "startswith": ScalarFunction(("string", "string"), 2, "boolean"),
    "endswith": ScalarFunction(("string", "string"), 2, "boolean"),
    "repeat": ScalarFunction(("string", "integer"), 2),
    "reverse": ScalarFunction(("string",), 1),
    "lpad": ScalarFunction(("string", "integer", "string"), 2),
    "rpad": ScalarFunction(("string", "integer", "string"), 2),
    "find": ScalarFunction(("string", "string", "integer"), 2, "integer"),
    "translate": ScalarFunction(("string", "string", "string"), 3),
    "levenshtein": ScalarFunction(("string", "string"), 2, "integer"),
    "url_protocol": ScalarFunction(("string",), 1),
    "url_host": ScalarFunction(("string",), 1),
    "url_path": ScalarFunction(("string",), 1),
    "url_query": ScalarFunction(("string",), 1),
    "url_fragment": ScalarFunction(("string",), 1),
    "as_date": ScalarFunction(("string", "string"), 2, "date"),
    "as_time": ScalarFunction(("string", "string"), 2, "time"),
    "as_timestamp": ScalarFunction(("string", "string"), 2, "timestamp"),
    "convert_base": ScalarFunction(("string", "integer", "integer"), 3),
}

# The five URL accessors, in catalog order: renderable where a backend has a
# rule (the local SQLite backend registers UDFs for them; DuckDB in this ibis
# build has none), so the per-source `functions` lists subtract them where the
# compiler cannot reach them.
URL_ACCESSORS: tuple[str, ...] = ("url_protocol", "url_host", "url_path", "url_query", "url_fragment")

# What the bundled DuckDB cannot render at all (verified per ibis 12.0.0 by
# compiling against a live connection): the URL accessors have no compilation
# rule there, and `convert_base` has none in any backend of this build.
DUCKDB_UNRENDERABLE = frozenset({"convert_base", *URL_ACCESSORS})
