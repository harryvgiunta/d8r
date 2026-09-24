"""Gate: deep-equality check of `parse_query(canonical)` vs the fixture.

Throwaway by design: prints `MATCH` and exits 0 on agreement, otherwise prints
both canonical JSON sides and exits 1.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from anyql.query import parse_query  # noqa: E402  (path set above)


def sort_deep(value):
    if isinstance(value, list):
        return [sort_deep(v) for v in value]
    if isinstance(value, dict):
        return {k: sort_deep(value[k]) for k in sorted(value)}
    return value


def canonical(value) -> str:
    return json.dumps(sort_deep(value), sort_keys=True)


def main() -> int:
    doc = (ROOT / "spec" / "canonical-query.anyql").read_text(encoding="utf-8").replace("\r\n", "\n")
    want = json.loads((ROOT / "spec" / "canonical-query.ast.json").read_text(encoding="utf-8"))
    got = parse_query(doc).to_json()
    if canonical(got) == canonical(want):
        print("MATCH")
        return 0
    print("MISMATCH")
    print("errors:", json.dumps(got["errors"]))
    print("want:", canonical(want))
    print("got: ", canonical(got))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
