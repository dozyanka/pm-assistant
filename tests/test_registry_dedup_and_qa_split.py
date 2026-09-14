"""Registry refresh/deduplication and resilient large-project Q&A regressions."""
from __future__ import annotations

import json
import tempfile
from contextlib import closing
import unittest
from pathlib import Path

from pm_app.db import Store
from pm_app.ollama import CHAT_MODEL, EMBED_MODEL, OllamaError
from pm_app.registry import merge_registry_candidates
from pm_app.service import Service, format_context

ROOT = Path(__file__).resolve().parents[1]


def row(mid: str, source: str, text: str) -> dict:
    return {
        "project_id": "one", "source_id": source, "message_id": mid,
        "source_type": "client_chat", "speaker": "Клиент", "text": text,
        "occurred_at": "2026-09-10T14:00:00+05:00", "timezone": "+05:00",
        "channel": "Созвон" if source == "bad" else "MAX · клиент",
    }


class SplitClient:
    def __init__(self):
        self.calls = []
        self.last_chat_stats = {}

    def models(self):
        return {CHAT_MODEL: "chat-v1", EMBED_MODEL: "embed-v1"}

    def chat(self, system, user, schema, max_tokens=2048):
        self.calls.append((system, user, schema))
        props = schema.get("properties", {})
        if "checks" in props:
            candidates = json.loads(user.split("\nCANDIDATES:\n", 1)[1])
            return {"checks": [{"claim_number": c["claim_number"], "verdict": "supported"}
                               for c in candidates]}
        if "message_ids" in props:
            return {"message_ids": []}
        if "BAD_SOURCE_MARKER" in user:
            raise OllamaError("synthetic malformed local answer")
        source_line = next(line for line in user.splitlines() if line.startswith('["M'))
        parsed = json.loads(source_line)
        return {"status": "supported", "claims": [{
            "text": "В источнике есть подтвержденная информация.",
            "evidence": [{"id": parsed[0], "quote": parsed[5][:120]}],
        }], "uncertainties": []}


class RegistryRefreshAndSplitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db.sqlite3")
        self.store.create_project("one", "One")

    def tearDown(self):
        self.tmp.cleanup()

    def test_same_source_snapshot_does_not_make_model_candidates_stale(self):
        self.store.import_rows([row("m1", "chat", "Согласовано решение.")], "one")
        rev = self.store.current("one")[0]
        entry = {
            "local_id": "R1", "stable_key": "stable-one", "kind": "DECISION",
            "subject": "Решение", "statement": "Согласовано решение.", "lifecycle": "agreed",
            "responsible": "", "deadline_text": "", "deadline_iso": "",
            "evidence": [{"revision_id": rev["id"], "quote": "Согласовано решение."}],
            "supersedes": [],
        }
        self.store.save_registry_snapshot("one", None, "fp1", "model1", [entry])
        # Simulate a stale flag produced by an older build over this exact source snapshot.
        with closing(self.store.connect()) as con, con:
            con.execute("UPDATE registry_entries SET stale=1 WHERE project_id='one'")
        self.store.save_registry_snapshot("one", None, "fp1", "model1", [])
        self.assertFalse(self.store.registry("one")["entries"][0]["stale"])

        self.store.save_registry_snapshot("one", None, "fp2", "model1", [])
        self.assertTrue(self.store.registry("one")["entries"][0]["stale"])

    def test_shared_evidence_merges_wording_variants(self):
        common = {"id": "M10", "quote": "Нужна грубая оценка дополнительного экспорта.", "revision_id": 10}
        base = {
            "local_id": "R1", "kind": "TASK", "lifecycle": "open", "responsible": "",
            "deadline_text": "", "deadline_iso": "", "supersedes": [],
            "_supersedes_uids": [],
        }
        left = {**base, "subject": "Оценка влияния дополнительного экспорта в личный кабинет",
                "statement": "Необходима грубая оценка трудозатрат и зависимостей для дополнительного экспорта.",
                "evidence": [dict(common)], "_uid": "C1:R1", "stable_key": "a"}
        right = {**base, "subject": "Оценка дополнительного экспорта для личного кабинета",
                 "statement": "Нужна грубая оценка дополнительного экспорта: трудозатраты и зависимости.",
                 "evidence": [dict(common)], "_uid": "C2:R1", "stable_key": "b"}
        merged, deduplicated = merge_registry_candidates([left, right])
        self.assertEqual(len(merged), 1)
        self.assertEqual(deduplicated, 1)

    def test_large_project_keeps_other_sources_when_one_model_answer_fails(self):
        rows = []
        for i in range(12):
            rows.append(row(f"bad-{i}", "bad", f"BAD_SOURCE_MARKER {i} " + "x" * 240))
            rows.append(row(f"good-{i}", "good", f"GOOD_SOURCE_MARKER {i} " + "y" * 240))
        self.store.import_rows(rows, "one")
        messages = self.store.current("one")
        self.assertGreater(len(format_context(messages)), 6400)
        for source in ("bad", "good"):
            self.assertLess(len(format_context(self.store.current("one", source))), 6400)

        result = Service(self.store, ROOT, SplitClient()).ask("one", "Что известно?")
        self.assertEqual(result["grouping"], "source")
        self.assertEqual(result["failed_source_count"], 1)
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["scope"]["scope_complete"])
        failed = next(g for g in result["groups"] if g["source_id"] == "bad")
        good = next(g for g in result["groups"] if g["source_id"] == "good")
        self.assertEqual(failed["answer"]["status"], "not_verified")
        self.assertEqual(good["answer"]["status"], "supported")
        self.assertTrue(good["answer"]["claims"])
        self.assertEqual(len(self.store.answers("one")), 1)

    def test_project_ui_is_compact_and_dataset_projects_stay_visible(self):
        html = (ROOT / "pm_app" / "static" / "index.html").read_text(encoding="utf-8")
        js = "\n".join(path.read_text(encoding="utf-8") for path in sorted((ROOT / "pm_app" / "static" / "js").glob("*.js")))
        removed = (
            "Короткая рабочая выжимка из реестра",
            "Кандидаты появляются только с точными основаниями",
            "Снимки прошлых ответов не используются как факты",
            "Люди, ролевые подписи и служебные адреса показаны отдельно",
            "Сообщения можно исправлять прямо здесь",
            "PM Assistant выполняет локальную операцию",
        )
        for phrase in removed:
            self.assertNotIn(phrase, html)
        self.assertIn("const visibleProjects=state.projects;", js)
        self.assertNotIn("datasetIds=new Set", js)
        self.assertNotIn("Это результаты поиска, а не подтверждённый ответ", js)
        self.assertNotIn("Техническая проверка:", js)
        self.assertNotIn('<span class="brand-mark"><b>P</b><i></i></span>', html)


if __name__ == "__main__":
    unittest.main()
