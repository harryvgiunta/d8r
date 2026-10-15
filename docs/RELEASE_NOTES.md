# D8R 0.1.0a1 — first public alpha

**Tag:** `v0.1.0-alpha.1` · **License:** MIT, Copyright (c) 2026 Harry Giunta

A keyboard-first terminal workspace for exploring local tables with contextual
completion, an Ibis-backed command language, saved query pages, results and SQL
panes, CSV export, reusable functions and optional AI assistance. Start with the
bundled synthetic demo or a small trusted Parquet file.

## Release preparation

- Added the MIT license and package attribution, license, alpha-status and project
  URL metadata.
- Fixed stale completion offers after caret movement: offer synchronization now
  runs through the message queue rather than waiting for a display refresh.
  Geometry placement still follows layout separately.
- Saved database/provider credentials use the native OS keyring, with explicit
  session-only use, guarded migration and no plaintext fallback. Query/history/chat
  data remains plaintext; see the safety boundaries below.
- Replaced the long README entrypoint with focused alpha installation and usage.
  Detailed reference content is preserved in the user guide.
- Added a real captured Parquet-to-CSV demo, explicit compatibility boundaries and
  an independent first-run feedback protocol.

## Verification performed

Maintainer-controlled checks on Windows with Python 3.13:

- `uv run pytest -q`: **1,606 passed, 13 skipped**. The run emitted 14,962 Ibis
  DuckDB `fetch_arrow_table()` deprecation warnings; it had no failures.
- `uv run python verify/check_canonical.py`: **MATCH**.
- Source distribution and wheel built successfully. Package checks confirmed MIT
  metadata/notice, bundled CSS and synthetic data, and source documentation.
- The installed wheel, outside the checkout, passed three caret/completion
  regression sequences; a real demo count returned 100; SQL compiled; one history
  entry and the draft restored without automatic query execution.
- Native Windows credential-store save/read/delete passed using a disposable
  value, with deletion verified.
- The recorded real-widget local Parquet workflow verified completion, exact
  grouped results, SQL, exported CSV and restart behavior.
- Gitleaks 8.30.1 found no detected secrets in the 13 reachable commits reviewed
  before this release or in the publication source snapshot. All 11 reachable
  historical Parquet/SQLite fixture objects matched deterministic synthetic
  generators. No CSV exports or local credential/environment files were tracked.
  This is a bounded audit, not proof that all sensitive material is detectable or
  absent from external forks, caches, backups or unreachable Git objects.

## Important limits

This is a **prerelease**, not production certification. macOS/Linux native stores
and terminal combinations are unverified. PostgreSQL opt-in integration tests
were not run for this release. D1 transport tests use mocked HTTP backed by real
local SQLite; generic Ibis adapters are not live-server-certified. A selectable
backend or compilable SQL dialect is not an execution guarantee.

D8R runs with your user account's permissions and is not a sandbox. Use trusted
files and least-privileged database accounts. Row limits do not bound query cost.
Optional AI sends context to the provider you configure. Workspaces, chats,
history and connection metadata remain plaintext. Failed legacy credential
migration leaves the original file unchanged, potentially still plaintext, and
can block unrelated saves to that file until migration succeeds.

## Install, try, report

```sh
git clone https://github.com/harryvgiunta/d8r.git
cd d8r
git checkout v0.1.0-alpha.1
uv sync --locked
uv run d8r
```

Python 3.13+ is required. Wheel and source-distribution downloads are attached,
along with `SHA256SUMS.txt`. The tagged checkout uses the exact `uv.lock`;
installing a wheel resolves its declared dependencies from the package index.

- [Recorded demo and reproduction steps](https://github.com/harryvgiunta/d8r/blob/v0.1.0-alpha.1/docs/DEMO.md)
- [Tested configurations, safety limits and first-run protocol](https://github.com/harryvgiunta/d8r/blob/v0.1.0-alpha.1/docs/ALPHA.md)
- [Full user guide](https://github.com/harryvgiunta/d8r/blob/v0.1.0-alpha.1/docs/USER_GUIDE.md)
- [Report bugs or first-run feedback](https://github.com/harryvgiunta/d8r/issues)

Independent human onboarding reports are still being sought. Include the
release, OS/Python/terminal, exact steps and expected versus actual results.
Do not post credentials, private queries/schema/data, or unredacted configuration
and workspace files.
