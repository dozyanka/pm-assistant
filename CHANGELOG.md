# Changelog

## 1.0.0 - 2026-09-14

First portfolio/public-source release.

### Product
- Source-grounded project Q&A with exact evidence.
- Structured project registry: tasks, decisions, promises, requirements, open questions and dependencies.
- Kanban task tracker with manager-controlled lifecycle states.
- Meeting-specific post-meeting generation.
- Participant-side inference with explicit PM confirmation.
- Message revisions, answer history and import history.
- Local Web, Windows Desktop and Remote Server/Client run modes.

### AI / data
- Self-hosted `qwen3.5:9b` through Ollama.
- `qwen3-embedding:0.6b` for semantic retrieval.
- Chunked processing for large project histories.
- Deterministic quote/source validation and secondary verification.
- SQLite project memory with versioned messages.

### Repository cleanup
- Split the large AI service into focused service modules behind a stable facade.
- Split the web stylesheet into maintainable CSS modules.
- Split the browser code into focused JavaScript files.
- Replaced patch-number test filenames with capability-oriented names.
- Removed generated caches, patch notes and validation artifacts from source control.
- Added GitHub Actions, security documentation, licensing and reproducible Windows build scripts.
