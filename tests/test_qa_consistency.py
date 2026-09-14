"""Contradictory structured-output recovery and fail-closed regressions."""
from __future__ import annotations
import copy
import tempfile
import time
import unittest
from pathlib import Path

from pm_app.db import Store
from pm_app.ollama import CHAT_MODEL, EMBED_MODEL, OllamaError
from pm_app.service import Service, answer_consistency_issue
from pm_app.web import AppState

ROOT = Path(__file__).resolve().parents[1]
EMPTY = {"status": "not_found", "claims": [], "uncertainties": []}


def row(text="Баннер к этому проекту не относится."):
    return {"project_id": "one", "source_id": "chat", "message_id": "m1",
            "source_type": "client_chat", "speaker": "PM", "text": text,
            "occurred_at": None, "timezone": None}


def valid_claim(text="Баннер к этому проекту не относится."):
    return {"status": "supported", "claims": [{"text": text,
        "evidence": [{"id": "M1", "quote": text}]}], "uncertainties": []}


def contradictory(text="Не нашёл ответа."):
    return {"status": "not_found", "claims": [{"text": text,
        "evidence": [{"id": "M1", "quote": "Баннер"}]}], "uncertainties": []}


def verified(verdict="supported"):
    return {"checks": [{"claim_number": 1, "verdict": verdict}]}


class Client:
    def __init__(self, replies):
        self.replies = copy.deepcopy(replies)
        self.calls = []
        self.last_chat_stats = {"prompt_tokens": 1, "output_tokens": 1, "done_reason": "stop"}
    def models(self):
        return {CHAT_MODEL: "chat-digest", EMBED_MODEL: "embed-digest"}
    def chat(self, system, user, schema, max_tokens=2048):
        kind = "locate" if "message_ids" in schema["properties"] else ("verify" if "checks" in schema["properties"] else "answer")
        self.calls.append((kind, system, user))
        expected, result = self.replies.pop(0)
        if expected != kind:
            raise AssertionError(f"Expected {expected}, got {kind}")
        return copy.deepcopy(result)


class ConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "memory.sqlite3")
        self.store.create_project("one", "One")
        self.store.import_rows([row()], "one")
    def tearDown(self):
        self.tmp.cleanup()

    def test_detector_is_narrow(self):
        self.assertEqual(answer_consistency_issue(contradictory()), "not_found_with_claims")
        self.assertEqual(answer_consistency_issue({"status":"supported","claims":[],"uncertainties":[]}), "answer_status_without_claims")
        self.assertIsNone(answer_consistency_issue(EMPTY))
        self.assertIsNone(answer_consistency_issue({"bad": "shape"}))

    def test_not_found_with_claims_is_repaired_then_normal_recovery_runs(self):
        c = Client([("answer", contradictory()), ("answer", EMPTY),
                    ("locate", {"message_ids": []})])
        r = Service(self.store, ROOT, c).ask("one", "Когда клиент передаст баннер?")
        self.assertEqual(r["status"], "not_found")
        self.assertEqual(r["claims"], [])
        self.assertEqual(r["diagnostics"]["consistency_repairs"], 1)
        self.assertTrue(r["diagnostics"]["recovery_used"])
        self.assertEqual([x[0] for x in c.calls], ["answer", "answer", "locate"])
        self.assertFalse(c.replies)

    def test_consistency_repair_can_return_supported_but_still_requires_verifier(self):
        text = row()["text"]
        c = Client([("answer", contradictory()), ("answer", valid_claim(text)),
                    ("verify", verified())])
        r = Service(self.store, ROOT, c).ask("one", "Что сказано про баннер?")
        self.assertEqual(r["status"], "supported")
        self.assertEqual(r["diagnostics"]["consistency_repairs"], 1)
        self.assertTrue(r["diagnostics"]["verification_ran"])
        self.assertEqual([x[0] for x in c.calls], ["answer", "answer", "verify"])

    def test_second_contradiction_fails_closed_without_saving(self):
        c = Client([("answer", contradictory()), ("answer", contradictory())])
        with self.assertRaisesRegex(OllamaError, "повторно вернула"):
            Service(self.store, ROOT, c).ask("one", "Когда клиент передаст баннер?")
        self.assertEqual(self.store.answers("one"), [])

    def test_consistency_repair_prompt_contains_full_scope_but_not_current_date(self):
        c = Client([("answer", contradictory()), ("answer", EMPTY),
                    ("locate", {"message_ids": []})])
        Service(self.store, ROOT, c).ask("one", "Когда клиент передаст баннер?")
        repair = c.calls[1]
        self.assertIn("CANDIDATE (untrusted draft)", repair[2])
        self.assertIn(row()["text"], repair[2])
        self.assertNotIn("2026-09-08", repair[2])


class PublicErrorTests(unittest.TestCase):
    def test_low_level_ollama_error_is_not_exposed_to_browser_job(self):
        with tempfile.TemporaryDirectory() as t:
            state = AppState(ROOT, Path(t))
            jid = state.start_job(lambda progress: (_ for _ in ()).throw(
                OllamaError("Contradictory answer: not_found contains claims. PRIVATE")))
            for _ in range(100):
                with state.job_lock:
                    job = dict(state.jobs[jid])
                if job["state"] != "running":
                    break
                time.sleep(0.01)
            self.assertEqual(job["state"], "error")
            self.assertIn("Локальная модель", job["error"])
            self.assertNotIn("Contradictory", job["error"])
            self.assertNotIn("PRIVATE", job["error"])


if __name__ == "__main__":
    unittest.main()
