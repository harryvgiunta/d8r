"""Bounded, redacted, in-memory AI diagnostics; never part of model context."""
from __future__ import annotations

import json
from collections import deque
from datetime import datetime, timezone
from urllib.parse import quote

MAX_LOG_CHARS = 262_144
MAX_ENTRY_CHARS = 65_536
MAX_LOG_ENTRIES = 256


class AIDiagnostics:
    def __init__(self) -> None:
        self._entries: deque[str] = deque()
        self._size = 0
        self._omitted = 0
        self._secrets: tuple[str, ...] = ()

    def protect(self, secrets: list[str]) -> None:
        # Tool arguments/results may contain JSON-escaped or URL-encoded text.
        variants = {variant for secret in secrets if secret for variant in (
            secret, json.dumps(secret, ensure_ascii=False)[1:-1],
            json.dumps(secret, ensure_ascii=True)[1:-1], quote(secret, safe=""),
        )}
        self._secrets = tuple(sorted(variants.union(self._secrets), key=len, reverse=True))

    def record(self, stage: str, detail: str) -> None:
        for secret in self._secrets:
            detail = detail.replace(secret, "[REDACTED]")
        # Strip terminal controls but retain normal query formatting.
        detail = "".join(ch for ch in detail if ch in "\n\t" or ch.isprintable())
        stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        entry = f"[{stamp}] {stage}\n{detail}\n\n"
        if len(entry) > MAX_ENTRY_CHARS:
            marker = "\n[Entry truncated]\n\n"
            entry = entry[:MAX_ENTRY_CHARS - len(marker)] + marker
        self._entries.append(entry)
        self._size += len(entry)
        while self._size > MAX_LOG_CHARS or len(self._entries) > MAX_LOG_ENTRIES:
            self._size -= len(self._entries.popleft())
            self._omitted += 1

    @property
    def text(self) -> str:
        prefix = f"[{self._omitted} older log entries omitted]\n\n" if self._omitted else ""
        return prefix + "".join(self._entries)

    def clear(self) -> None:
        self._entries.clear()
        self._size = self._omitted = 0
        self._secrets = ()
