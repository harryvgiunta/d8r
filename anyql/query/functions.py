"""Scalar call signatures shared by parsing, Ibis compilation, and completion."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ArgumentKind = Literal["string", "integer", "any"]
ResultKind = Literal["string", "integer", "boolean"]


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
}
