# D8R public alpha: scope, limitations and feedback

**Version:** `0.1.0a1` · **Git tag:** `v0.1.0-alpha.1` · **License:** MIT

This is an early public alpha for evaluating a terminal-first query workflow,
not a production-readiness or universal-backend claim. Start with synthetic data,
keep your own backups, and use least-privileged database accounts.

- [README and installation](../README.md)
- [Demo workflow and evidence](DEMO.md)
- [User guide](USER_GUIDE.md)
- [Compiler and backend compatibility boundaries](COMPATIBILITY.md)

## Release scope

The alpha includes the document editor and contextual completion, query/SQL/results
panes, bundled synthetic DuckDB demo, local Parquet sources, saved source-specific
pages/history, and optional database and AI-provider connections. Compilation and
execution are separate: choosing a SQL dialect does not connect to that database.

Release preparation includes native OS credential-store persistence through
`keyring`, explicit Remember-off session-only credentials, legacy credential
migration, and completion fixes. These changes do not establish compatibility
with every OS credential service, terminal, driver or live database. Consult
the release's verification record rather than treating implementation scope
as test evidence.

## What has and has not been exercised

**Evidence boundary:** the following describes maintainer-controlled checks, not
independent customer validation. **No completed external user testing is claimed.**
The protocol below is an invitation to collect that evidence, not a report of
people who have already performed it. Headless TUI checks do not certify physical
keyboard bindings, clipboard behavior or rendering in every terminal.

### OS and runtime

| Environment | Alpha evidence | Boundary |
| --- | --- | --- |
| Native Windows, Python 3.13 | Native OS credential-store put/get/delete smoke exercised; installed-package headless TUI query, compile and restart exercised | Not an exhaustive Windows/terminal matrix or an independent human usability test |
| macOS, Python 3.13 | Unverified | Keychain integration is implemented through `keyring`, not exercised for this alpha |
| Linux, Python 3.13 | Unverified | Secret Service/KWallet integration is implemented through `keyring`, not exercised for this alpha; an unlocked service and D-Bus session are required |
| Other Python versions | Unverified; package requires Python `>=3.13` | A declared lower bound does not certify every newer Python or dependency wheel |
| Other architectures, terminal emulators, SSH/headless desktop credential services | Unverified | Native drivers, keyboard mapping, clipboard and vault availability vary |

### Data sources and compilation

| Path | Available evidence | Not established |
| --- | --- | --- |
| Bundled demo and vendor-shaped mocks | Real in-process DuckDB over synthetic local data; automated local execution coverage and installed demo smoke | Vendor-shaped mocks are not PostgreSQL/MySQL/Snowflake/BigQuery servers |
| Local Parquet | Real DuckDB reader and automated local-source coverage | Arbitrary file sizes, schemas, storage devices or performance guarantees |
| Local SQLite / D1 snapshot | Real local SQLite execution coverage | Hosted Cloudflare compatibility or production-data validation |
| Cloudflare D1 HTTP path | `httpx.MockTransport` tests execute generated SQL on real UDF-less local SQLite | **No live Cloudflare service certification**; transport, authentication, service limits and deployment behavior are not proved by these tests |
| Dedicated PostgreSQL connection | Implemented adapter and opt-in integration tests | **PostgreSQL opt-in tests were not run for this alpha**; no live-server certification |
| Other advertised Ibis adapters | Implemented connection forms/adapters and compiler-side capability checks | No live certification of those backends; optional drivers, native libraries, Java/runtime and provider prerequisites still apply |
| SQL dialect selection | Representative offline compiler probes for 19 targets; PySpark requires its real Spark runtime | Rendering does not prove that all expressions compile, that SQL executes, or that results match across engines |

The detailed [compatibility tables](COMPATIBILITY.md) label mocked and opt-in
columns next to their claims. Server versions, extensions, UDFs, data types,
permissions and provider behavior can change the result. Follow the canonical
[development PostgreSQL instructions](USER_GUIDE.md#local-postgresql-with-docker)
for a disposable integration environment; do not use production data to try tests.

## Known limitations and safety boundaries

### Credentials, session-only use and plaintext data

- Remembered passwords, tokens, AI keys and generic backend secret/advanced options
  use the native OS credential store through Python `keyring`. D8R rejects null,
  plaintext and arbitrary third-party credential backends; it does not provide a
  fallback plaintext vault. An installed library alone cannot provision or unlock
  your OS credential service. Native entry-size limits also apply.
- When a suitable vault is unavailable, turn **Remember off** explicitly for
  session-only credentials. They are not durable and must be re-entered after
  restart. Locked/missing saved credentials can block reconnect or AI requests.
  Session-only use is not a migration repair and does not erase old files.
- Legacy plaintext migration writes and verifies vault entries before replacing
  JSON secret fields with references. **If migration fails, the original file
  remains unchanged and may still contain plaintext secrets.** It does not create
  a plaintext backup, but cannot erase existing backups, snapshots or exports.
- **Migration failure also limits persistence:** saves to an affected legacy
  `memory.json` or `settings.json` may need to secure its existing secrets again.
  Unrelated function/preference changes can therefore fail to persist while the
  vault remains unavailable. Do not assume a visible edit was saved; heed errors,
  preserve unsaved work securely, unlock/configure the vault and restart to retry.
- Queries, history, AI chats, connection metadata, local paths and exported results
  remain **plaintext**, normally under `~/.d8r` (or `D8R_DATA_DIR`). They can contain
  secrets you type into documents even when credential fields use the vault.
  Protect the directory, exports and backups with OS permissions; do not attach
  your data directory to a public issue. Windows uses filesystem ACLs.
- Credential references are tied to the data directory and OS credential store.
  Copying JSON to another directory, user account or machine does not transfer
  vault secrets. Do not assume a configuration backup is a portable credential
  backup. External file changes require restart; invalid files are preserved and
  unsafe saves are blocked rather than silently overwriting them.

### Workflow and integration limits

- D8R is a local application, **not a sandbox**. Native drivers and file access run
  with your account's permissions. Open trusted files as a normal, non-admin user.
- One existing local `.parquet`/`.pq` file is opened per source as table `data`.
  URLs, UNC paths and wildcard inputs are not supported. Reopening requires the
  file to remain accessible at its saved path; D8R does not copy it into a backup.
- Drafts/pages and successful history persist; result grids, temporary tables and
  transaction state do not. Restart may reconnect the selected source but does not
  execute its saved query. A missing source fails rather than substituting demo data.
- Generic adapters only enable transactions/temp tables for their supported local
  DuckDB/SQLite implementations. Dedicated PostgreSQL has its own transaction path;
  D1 HTTP does not retain session transactions or temporary tables.
- Backend-specific function gaps and runtime prerequisites remain. PySpark needs a
  real Spark session; Flink needs a compatible separately installed PyFlink/Java
  runtime. Installing an extra is not the same as provisioning a working service.
- Use read-only roles where possible. PostgreSQL's default `prefer` SSL mode does
  not guarantee encryption or server identity; use provider-appropriate verified
  TLS. Optional AI sends requested context to the configured provider. Leave it
  unconfigured if data must remain local.
- This alpha carries no production support or response-time guarantee. Report
  reproducible defects and compatibility observations without sharing credentials.

## Ten-minute independent first-run protocol

Allow about ten minutes **after Python 3.13 and dependencies are available**;
network downloads may take longer. Do this in a new directory with no private
files, no existing D8R configuration and no database/AI credentials. This procedure
is a test invitation; it has not been completed by an external user on our behalf.

### 1. Fresh install and isolated workspace (minutes 0–3)

Use the [release](https://github.com/harryvgiunta/d8r/releases/tag/v0.1.0-alpha.1)
artifacts or the tagged source archive below, not an older working checkout.
To use the wheel, download `d8r-0.1.0a1-py3-none-any.whl` from that release and
replace the `pip install` URL below with its local file path. Dependencies are
installed from the Python package index; the source checkout's `uv sync --locked`
is the path that uses the repository's exact dependency lock.

**Windows PowerShell:**

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install "https://github.com/harryvgiunta/d8r/archive/refs/tags/v0.1.0-alpha.1.zip"
$env:D8R_DATA_DIR = Join-Path (Get-Location) "alpha-home"
.venv\Scripts\python -m d8r
```

**macOS/Linux (instructions to try, not verified compatibility):**

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install "https://github.com/harryvgiunta/d8r/archive/refs/tags/v0.1.0-alpha.1.zip"
export D8R_DATA_DIR="$PWD/alpha-home"
.venv/bin/python -m d8r
```

If installation fails, retain the exact command and redacted error and report
that step; do not work around it with production credentials or an administrator
account. Keep the same shell and `D8R_DATA_DIR` for the restart check.

### 2. Run and compile the bundled demo (minutes 3–5)

1. Leave the source on **Local demo · DuckDB + Parquet** and use the initial query.
2. Press **F5** (or Ctrl+Enter). Observe actual rows in Results and a successful
   History entry. Record any error instead of assuming a populated pane is a pass.
3. Press **F6** (or Ctrl+K). Observe SQL without an additional query execution or
   History entry. Compare with the [demo workflow](DEMO.md).
4. Note terminal/key-binding problems separately from query failures. Return to
   the query editor before quitting with Ctrl+Q.

### 3. Open a tiny synthetic Parquet file (minutes 5–8)

With the app closed, create this three-row file using the environment's Python.
On Windows run the following; on macOS/Linux replace `.venv\Scripts\python` with
`.venv/bin/python`:

```sh
.venv\Scripts\python -c "import pyarrow as pa; import pyarrow.parquet as pq; pq.write_table(pa.table({'id': [1, 2, 3], 'amount': [10, 20, 30]}), 'alpha-synthetic.parquet')"
```

Relaunch with the same `python -m d8r` command. Press **Ctrl+O**, choose
**Local Parquet file · DuckDB**, enter the absolute path to
`alpha-synthetic.parquet`, and choose **Add**. Replace the editor document with:

```d8r
\from data
\select id, amount
\order id
\limit 10
```

Run with F5. Expect exactly `(1, 10)`, `(2, 20)`, `(3, 30)` in `id, amount`
order. Compile with F6 and inspect the SQL. Try column completion and note any
surprising suggestion, insertion or missing suggestion. Do not substitute a real
private file for this synthetic fixture in your report.

### 4. Save, close and reopen (minutes 8–9)

The query draft autosaves; use **Pages** to find the current source's page and,
if desired, give it a recognizable name. Quit from the query view and relaunch
with the same environment and data directory. Confirm the selected local file
and document/page are restored. The old result grid is not expected to persist,
and startup should not run the query. Press F5 yourself and confirm the same
three rows. Report any save warning or mismatch rather than counting it as a pass.

### 5. Send an independent report (minutes 9–10)

Open a [GitHub issue](https://github.com/harryvgiunta/d8r/issues) for a reproducible
bug or first-run feedback. Include successes and failures; an honest failed
installation is useful evidence. Include:

- Release/tag and installation source (wheel or tagged archive), exact command,
  OS/version/architecture, Python version and terminal application/version.
- Which numbered steps you attempted, expected versus actual result, and the
  **exact failure message** or a small redacted screenshot/traceback.
- The smallest synthetic query/file recipe needed to reproduce it; backend and
  driver versions if reporting a separate database issue, without connection
  secrets. State whether it used a live service or a mock/local snapshot.
- Whether you used a fresh `D8R_DATA_DIR`, whether Remember was on/off for any
  separate credential test, and whether the issue survives restart.

**Redact passwords, tokens, API keys, authorization headers, credential references,
private hostnames/paths, query literals and customer data.** Do not upload your
workspace/settings files, credential store or production data. If safe reproduction
is impossible, describe the behavior without posting the sensitive material. For a
potential security issue, do not publish an exploit or secret in a public issue;
use GitHub private vulnerability reporting if available, otherwise ask for a private
contact without disclosing the sensitive details.
