"""Relation-name syntax, separate from bare aliases and local identifiers."""

from __future__ import annotations

import re

IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"
_NAME = rf'(?:{IDENTIFIER}|"(?:[^"]|"")+")'
RELATION_NAME = rf"{_NAME}(?:\.{_NAME})?"
_RELATION_RE = re.compile(rf"({_NAME})(?:\.({_NAME}))?")


def relation_parts(text: str) -> tuple[str, ...] | None:
    """Decode one or two relation components; doubled quotes escape a quote."""
    match = _RELATION_RE.fullmatch(text)
    if match is None:
        return None
    return tuple(
        part[1:-1].replace('""', '"') if part.startswith('"') else part
        for part in match.groups() if part is not None
    )


def is_relation_name(text: str) -> bool:
    """Whether text is a bare or schema-qualified relation, optionally quoted."""
    return _RELATION_RE.fullmatch(text) is not None


def relation_name(schema: str, table: str) -> str:
    """Spell raw physical names as one safely quoted schema.table identifier."""
    def quote(part: str) -> str:
        return part if re.fullmatch(IDENTIFIER, part) else '"' + part.replace('"', '""') + '"'

    return f"{quote(schema)}.{quote(table)}"


def relation_basename(text: str) -> str:
    """Decode the final component; opaque legacy registry names stay unchanged."""
    parts = relation_parts(text)
    return parts[-1] if parts else text
