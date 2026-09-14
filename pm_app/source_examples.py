"""Explicit importers for source-document examples used as independent test projects."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .db import parse_jsonl

SOURCE_PROJECT_1_ID = "urbankey-source-example-1"


def load_source_project_1(root: Path) -> tuple[list[dict], dict]:
    """Load exactly the source document section 'Проект 1. UrbanKey'.

    The fixture is intentionally independent from the normal `urbankey` demo project so
    a tester can reset/compare it without mixing stress-test rows or other source projects.
    """
    directory = Path(root) / "samples" / "source_project_1"
    try:
        raw = (directory / "messages.jsonl").read_bytes()
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(
            "Исходный пример №1 отсутствует или повреждён. Распакуйте патч 07 в корень проекта."
        ) from exc

    if hashlib.sha256(raw).hexdigest() != manifest.get("messages_sha256"):
        raise ValueError("Контрольная сумма исходного примера №1 не совпадает. Импорт отменён.")

    rows = parse_jsonl(raw.decode("utf-8-sig"))
    if manifest.get("project_id") != SOURCE_PROJECT_1_ID:
        raise ValueError("Некорректный project_id в манифесте исходного примера №1.")
    if len(rows) != manifest.get("message_count") or len(rows) != 34:
        raise ValueError("Исходный пример №1 должен содержать ровно 34 сообщения UrbanKey.")
    if {r["project_id"] for r in rows} != {SOURCE_PROJECT_1_ID}:
        raise ValueError("В исходный пример №1 попали сообщения другого проекта.")
    if {r.get("original_project_id") for r in rows} != {"urbankey"}:
        raise ValueError("Нарушена связь исходного примера №1 с UrbanKey.")
    if len({r["source_id"] for r in rows}) != manifest.get("source_count") or manifest.get("source_count") != 4:
        raise ValueError("Исходный пример №1 должен содержать четыре исходных источника.")
    if len({r["message_id"] for r in rows}) != len(rows):
        raise ValueError("В исходном примере №1 повторяются message_id.")
    forbidden_topics = {r.get("topic_id") for r in rows if r.get("topic_id")}
    if forbidden_topics:
        raise ValueError("Исходный пример №1 не должен содержать смешанный topic_id/stress-test.")
    return rows, manifest
