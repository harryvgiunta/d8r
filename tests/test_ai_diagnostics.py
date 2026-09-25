"""Diagnostics cannot leak known credentials or grow without bound."""
import json
from urllib.parse import quote

from d8r.ai import diagnostics
from d8r.ai.diagnostics import AIDiagnostics


def test_known_credentials_are_redacted_before_truncation_and_copy():
    trace = AIDiagnostics()
    secret = 'private-key-"\\\n\u00e9'
    trace.protect([secret, ""])
    variants = [secret, json.dumps(secret, ensure_ascii=False)[1:-1],
                json.dumps(secret, ensure_ascii=True)[1:-1], quote(secret, safe="")]
    trace.record("Tool arguments", "\n".join(variants))
    assert trace.text.count("[REDACTED]") == len(variants)
    assert "private-key" not in trace.text
    trace.record("Large model response", "x" * (diagnostics.MAX_ENTRY_CHARS - 80) + secret * 20)
    assert "private-key" not in trace.text
    assert "[Entry truncated]" in trace.text
    trace.protect(["replacement-key"])
    trace.record("Later model response", secret + " replacement-key")
    assert "private-key" not in trace.text and "replacement-key" not in trace.text


def test_diagnostics_evict_old_entries_but_keep_the_latest_failure():
    trace = AIDiagnostics()
    trace.record("Start", "oldest-attempt")
    for index in range(8):
        trace.record("Schema", f"lookup-{index}\n" + "x" * diagnostics.MAX_ENTRY_CHARS)
    trace.record("Failure", "last-failure: unsupported operation")
    assert "oldest-attempt" not in trace.text
    assert "older log entries omitted" in trace.text
    assert "last-failure: unsupported operation" in trace.text
    assert len(trace.text) <= diagnostics.MAX_LOG_CHARS + 100
    trace.clear()
    assert trace.text == ""


def test_many_small_events_are_bounded_and_controls_are_removed():
    trace = AIDiagnostics()
    trace.record("Start", "oldest-attempt")
    for _ in range(diagnostics.MAX_LOG_ENTRIES):
        trace.record("Response", "line\n\tvalue\x1b[2J\x00")
    assert "oldest-attempt" not in trace.text
    assert "1 older log entries omitted" in trace.text
    assert "\x1b" not in trace.text and "\x00" not in trace.text
    assert "line\n\tvalue" in trace.text
