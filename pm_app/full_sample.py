"""Explicit, idempotent import of the supplied full test corpus, never on startup."""
from __future__ import annotations
import hashlib
import json
from collections import Counter
from pathlib import Path
from .db import parse_jsonl


def load_full_sample(root: Path) -> tuple[list[dict], dict]:
    directory = Path(root) / 'samples' / 'full_xpage'
    try:
        raw = (directory / 'messages.jsonl').read_bytes()
        manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError('Full sample files are missing or invalid. Extract patch 03 into the project root.') from exc
    if hashlib.sha256(raw).hexdigest() != manifest.get('messages_sha256'):
        raise ValueError('Full sample checksum mismatch. No messages were imported.')
    rows = parse_jsonl(raw.decode('utf-8-sig'))
    if len(rows) != manifest.get('message_count') or len(rows) != 102:
        raise ValueError('Full sample must contain exactly 102 source messages.')
    if {r['project_id'] for r in rows} != {manifest.get('project_id')}:
        raise ValueError('Unexpected project in the full example.')
    counts = Counter(r.get('topic_id') for r in rows)
    expected = {t['id']: t['message_count'] for t in manifest['topics']}
    if counts != expected:
        raise ValueError('Full sample topic coverage mismatch.')
    if len({r['source_id'] for r in rows}) != manifest['source_count']:
        raise ValueError('Full sample source coverage mismatch.')
    if len({r['message_id'] for r in rows}) != len(rows):
        raise ValueError('Full sample contains repeated message IDs.')
    return rows, manifest
