"""Shared alias semantics for anyQL — ported from an earlier TypeScript build.

The aggregate auto-alias rule is one implementation shared by the payload
builder, order-target resolution, and `\\order` completion. The engine
re-derives the same name independently when the payload alias is null. The
contract is `docs/AST.md`, pinned by `spec/canonical-query.ast.json`.
"""

from __future__ import annotations

import re

from .ast import SelectItem

_NON_ALIAS = re.compile(r"[^a-z0-9_]")


def auto_alias(fn: str, arg: str) -> str:
    """Derived output name for an aggregate select item with no explicit alias.

    `<arg>_<fn>`, both lowercased; a qualified aggregate argument contributes
    only its last segment (`sum(users.amount)` -> `amount_sum`); the result is
    sanitized to `[a-z0-9_]`. An explicit `as` alias always wins over this.
    """
    last = arg[arg.rfind(".") + 1 :]
    return _NON_ALIAS.sub("_", f"{last.lower()}_{fn.lower()}")


def effective_alias(item: SelectItem) -> str | None:
    """The output name a select item answers to downstream (orderBy, the result).

    Its explicit alias, else a literal or scalar call's original text, else the
    derived alias for aggregates, temporals and regex calls, else the rank name,
    else `None` (a plain column keeps its own name).
    """
    if item.alias:
        return item.alias
    if item.literal is not None or item.scalar is not None:
        return item.raw
    if item.rank:
        return item.rank.fn
    if item.aggregate:
        return auto_alias(item.aggregate.fn, item.aggregate.arg)
    if item.temporal:
        return auto_alias(item.temporal.fn, item.temporal.arg)
    if item.regex:
        return auto_alias(item.regex.fn, item.regex.arg)
    return None
