# Architecture

PM Assistant treats the LLM as an analysis layer, not as the source of truth.
The source of truth is the original project communication plus structured application state in SQLite.

```text
PM / user
   |
   +--> Web UI / Windows Desktop / Remote Client
                     |
                     v
              Python HTTP backend
                     |
        +------------+-------------+
        |                          |
        v                          v
     SQLite                    Ollama API
(project memory)         127.0.0.1:11434
                                   |
                      +------------+------------+
                      |                         |
                      v                         v
                Qwen3.5 9B            Qwen3 Embedding 0.6B
              analysis/generation       semantic retrieval
```

## Evidence-first Q&A

```text
selected project scope
      -> context/retrieval
      -> Qwen structured draft
      -> JSON/schema validation
      -> source-ID validation
      -> exact quote validation
      -> verifier pass
      -> answer + clickable evidence
```

If a large scope does not fit one request, it is processed by source rather than silently truncating project material.

## Registry
The registry pipeline scans the complete selected history in overlapping chunks, extracts candidates, validates evidence, verifies candidates, merges duplicates and stores a snapshot. Manager review state is separate from the business lifecycle state.

## Post-meeting
Post-meeting generation uses one selected meeting/session. Participant-side information is used when available; uncertain roles can remain unresolved for manager confirmation.

## Code organization
- `pm_app/service.py` - stable facade.
- `pm_app/services/qa.py` - grounded Q&A orchestration.
- `pm_app/services/registry_service.py` - registry extraction/verification flow.
- `pm_app/services/post_meeting.py` - meeting and post-meeting flow.
- `pm_app/services/participants.py` - participant-side inference.
- `pm_app/services/search.py` - hybrid semantic/lexical search.
- `pm_app/service_support.py` - shared validated schemas/context helpers.
- `pm_app/db.py` - SQLite gateway and migrations.
- `pm_app/web.py` - HTTP/auth/API boundary.
