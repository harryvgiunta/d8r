"""Presentation-only D8R syntax colors using TextArea's public line hook.

Color visible lines without parsing, querying a source, or rewriting the document.
Incomplete quotes and calls are ordinary editor input, not validation errors.
"""

from __future__ import annotations

import re

from rich.style import Style
from rich.text import Text
from textual.widgets import TextArea

from d8r.query.identifiers import IDENTIFIER

_TOKENS = re.compile(
    r"(?P<string>'(?:[^']|'')*(?:'|$)|\"(?:[^\"]|\"\")*(?:\"|$))"
    rf"|(?P<command>\\{IDENTIFIER})"
    rf"|(?P<parameter>(?<![A-Za-z0-9_])@{IDENTIFIER})"
    r"|(?P<number>(?<![A-Za-z0-9_])(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)"
    rf"|(?P<word>{IDENTIFIER})"
    r"|(?P<operator>!=|>=|<=|!~|[=<>~+*/%-])"
    r"|(?P<punctuation>[(),.])"
)
_KEYWORDS = frozenset(
    "as on and or not in is null true false like ilike between all distinct "
    "lateral cross asc desc when then else end over partition by order rows range "
    "unbounded preceding following current row to".split()
)
_CALL_TAIL = re.compile(r"\s*\(")


class QueryTextArea(TextArea):
    """D8R document editor; caret, selection, wrapping and undo remain TextArea's."""

    COMPONENT_CLASSES = {
        "query--command", "query--keyword", "query--function", "query--string",
        "query--number", "query--parameter", "query--operator", "query--punctuation",
    }
    DEFAULT_CSS = """
    QueryTextArea {
        & .query--command, & .query--keyword {
            color: $text-primary;
            text-style: bold;
        }
        & .query--function { color: $text-accent; text-style: bold; }
        & .query--string { color: $text-success; }
        & .query--number { color: $text-warning; }
        & .query--parameter { color: $text-secondary; text-style: italic; }
        & .query--operator { color: $text-accent; }
        & .query--punctuation { color: $text-muted; }
    }
    """

    def get_component_rich_style(
        self, *names: str, partial: bool = False, default: Style | None = None,
    ) -> Style:
        # TextArea applies the cursor-line style after get_line. Inheriting the
        # editor's foreground there would paint over every syntax token.
        return super().get_component_rich_style(
            *names, partial=partial or names == ("text-area--cursor-line",), default=default,
        )

    def get_line(self, line_index: int) -> Text:
        line = super().get_line(line_index)
        text = line.plain
        for token in _TOKENS.finditer(text):
            kind = token.lastgroup
            start, end = token.span()
            if kind == "word":
                # Qualified column names are identifiers even when a component
                # has a keyword spelling, e.g. events.order or order.amount.
                if (start and text[start - 1] == ".") or text[end:end + 1] == ".":
                    continue
                if token.group().lower() in _KEYWORDS:
                    kind = "keyword"
                elif _CALL_TAIL.match(text, end):
                    kind = "function"
                else:
                    continue
            elif kind == "command" and _CALL_TAIL.match(text, end):
                kind = "function"
            line.stylize(
                self.get_component_rich_style(f"query--{kind}", partial=True), start, end,
            )
        return line
