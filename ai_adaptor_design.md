# AI Adaptor Design for D8R

## Recommendation: Simple write (AI generates `\\command` documents)

**Why**: D8R's architecture is document-centric. The contract is
`document text → AST → payload → ibis → rows → widgets`. AI just contributes
document text that flows through the existing pipeline.

**Key principles**:
- Keep AI writing documents, not bypassing them
- The `\` command palette + intellisense already assists AI-generated commands
- If RPC needed later, plug into engine seams (`expression.py`, `execute.py`)
  optionally, not as default path

**If RPC becomes needed**: plug into these seams rather than bypassing document layer:
- `d8r/query/functions.py` — SCALAR_FUNCTIONS registry
- `d8r/engine/expression.py` — PayloadError, build(), compile_sql()
- `d8r/engine/execute.py` — execute(), execute_remote()

**Avoid**: Bypassing the parser. The document layer provides syntax validation,
schema awareness, capability-driven completion, and error reporting.