# Introducing D8R: a keyboard-first data workspace in your terminal

By Harry Giunta

D8R (pronounced “Datuhr”) is a terminal IDE for exploring tables with a small
command language, contextual completion and a results grid. The first public
alpha is available under the MIT license.

[Get the alpha](https://github.com/harryvgiunta/d8r/releases/tag/v0.1.0-alpha.1)
· [Source and installation](https://github.com/harryvgiunta/d8r)

![D8R opening synthetic Parquet, completing a query, showing results and SQL, and exporting CSV](https://raw.githubusercontent.com/harryvgiunta/d8r/v0.1.0-alpha.1/docs/assets/d8r-demo.gif)

*These are real headless Textual captures against the real DuckDB engine, held
as a short slideshow—not a mockup or a performance benchmark.*

## Start with a file, not infrastructure

The bundled demo works without a database account or an AI provider. You can
also open a local Parquet file with Ctrl+O. It appears as a table named `data`,
ready for a query like this:

```text
\from data
\select event_type, count(*) as events
\group event_type
\order event_type
```

Against the bundled synthetic events file, that returns four event types with
25 events each. Ctrl+Enter runs the query; Ctrl+K shows the generated SQL
without executing it. The results grid can export its buffered rows to CSV.
Query drafts and successful history return when you reopen the app, but the
query does not automatically run again.

## Why not just SQL?

D8R is deliberately not an arbitrary SQL editor. The backslash commands give
completion an explicit context: choose a table after `\from`, fields after
`\select`, and predicates after `\where`. The document becomes a structured
AST and then an Ibis expression; Ibis handles SQL rendering. Textual provides
the terminal interface.

The tradeoff is learning a small language that does not expose every SQL
feature. What you get is one editing model for queries, completion, validation,
saved pages and reusable parameterized functions. Your data source and SQL
rendering dialect remain separate: choosing a dialect does not change where a
query runs.

## An alpha, with explicit boundaries

The strongest starting point is the local demo or a small trusted Parquet file.
Windows/Python 3.13 has received installed-package headless checks and a native
credential-store smoke. macOS/Linux and the full set of remote database
adapters are not certified. A backend appearing in a picker is not proof of
live-server compatibility.

AI assistance is optional and uses a provider you configure. If you enable it,
queries and requested context may leave your machine. Remembered credentials
use the OS keyring, but query drafts, history, chats and connection metadata
remain local plaintext. D8R is not a sandbox; start with synthetic data and
least-privileged database accounts.

The [alpha guide](https://github.com/harryvgiunta/d8r/blob/v0.1.0-alpha.1/docs/ALPHA.md)
details what has and has not been exercised.

## Try it and tell me where you get stuck

Install Git and [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```sh
git clone https://github.com/harryvgiunta/d8r.git
cd d8r
git checkout v0.1.0-alpha.1
uv sync --locked
uv run d8r
```

I am looking for first-time users to try the
[ten-minute synthetic-data walkthrough](https://github.com/harryvgiunta/d8r/blob/v0.1.0-alpha.1/docs/ALPHA.md#ten-minute-independent-first-run-protocol).
An installation failure or confusing interaction is useful feedback—not just a
successful demo. Share your OS, Python and terminal versions, the steps you
tried, and what surprised you in
[GitHub Issues](https://github.com/harryvgiunta/d8r/issues). Please keep credentials
and private data out of reports.
