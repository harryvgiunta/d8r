# Local Parquet to results, SQL and CSV

![Six captured D8R screens showing a local Parquet file, contextual completion, grouped results, generated SQL and CSV export](assets/d8r-demo.gif)

This 21-second slideshow uses real Textual widget captures from the alpha, driven
headlessly against the real Ibis/DuckDB engine. It is not a hand-drawn mockup or a
recording of a human usability session. Only bundled synthetic data is used; no
AI provider, credentials or remote database is needed. Frames are held for
readability, so their timing is not a performance measurement.

## Reproduce it

Follow the [tagged installation instructions](../README.md#install-the-alpha-and-run-a-query)
and launch D8R from the repository root. Use a reasonably wide terminal; the
capture uses 130 columns by 46 rows.

1. Press **Ctrl+O**. Choose **Local Parquet file · DuckDB** and enter
   `d8r/engine/data/events.parquet` as the file path. Give it the display name
   **Synthetic events** and choose **Add**. Local files are exposed as table
   `data`, regardless of their original filename.
2. Enter `\from data`, then start the next line with `\select ev`. The popup
   offers `event_type` from the file's schema. **Tab** accepts the highlighted
   suggestion. Complete the document as follows:

   ```d8r
   \from data
   \select event_type, count(*) as events
   \group event_type
   \order event_type
   ```

3. Press **Ctrl+Enter** or **F5**. Results contain four rows: `click`, `purchase`,
   `signup`, and `view`, each with **25** events. One successful run appears in
   History.
4. Press **Ctrl+K** or **F6**. The SQL tab shows Ibis-generated SQL with grouping.
   Compilation does not execute the query or add another History entry.
5. Select the **Results** tab, then **Export CSV…**. Enter a new destination path
   and choose **Export CSV**. The file has the headers `event_type,events` and
   the same four result rows. Export writes the captured result buffer; it does
   not rerun the query. Do not overwrite a file you want to keep.
6. Quit from the query workspace with **Ctrl+Q**, then reopen D8R. The selected
   source, query draft and successful history return. The old result grid does
   not persist, and startup does not execute the query. Press Run yourself to
   refill the grid.

The capture run checked the exact grouped rows, generated SQL, exported CSV,
one-entry History, and restart behavior. It also exercised completion acceptance.
See [the alpha guide](ALPHA.md) for the limits of that evidence and an independent
first-run feedback protocol.

## Why use commands instead of a SQL editor?

The command prefix gives completion an explicit context: tables after `\from`,
columns after `\select`, and predicates after `\where`. A document is parsed
into a structured AST, then composed into Ibis expressions; the engine generates
SQL. This makes completion, validation and reusable query functions share the
same language model.

The tradeoff is learning D8R syntax rather than pasting arbitrary SQL. Not all
SQL constructs or backend capabilities are exposed. Choosing a SQL dialect
changes rendering, not the connected database or where Run executes.
