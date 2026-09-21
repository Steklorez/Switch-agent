# Project guidance

Read `CLAUDE.md` for the project's release, changelog and History UI rules.

Use the installed Graphify tool to investigate code structure and relationships:

- The local code graph is `graphify-out/graph.json`.
- Start with `graphify explain "Symbol"` or `graphify path "A" "B"` for relevant symbols; verify conclusions in the source before editing.
- Refresh a stale code graph with `graphify update . --no-cluster` (local AST extraction, no LLM required).
- This graph covers code relationships, not a complete semantic analysis of documentation or images. Read relevant documents directly.
- Keep generated graph artifacts local unless explicitly asked to commit them.
