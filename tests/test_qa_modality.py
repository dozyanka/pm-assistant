"""Q&A verifier regressions for promises, plans and completion claims."""
from __future__ import annotations
import tempfile
import unittest
from pathlib import Path

from pm_app.answer_policy import infer_claim_kind, verifier_recheck_allowed
from pm_app.db import Store
from pm_app.service import Service
from test_answer_recovery import ScriptedClient, verified
from test_app import row

ROOT = Path(__file__).resolve().parents[1]


def answer(text: str, quote: str, label: str = "M1") -> dict:
    return {"status": "supported", "claims": [{"text": text,
        "evidence": [{"id": label, "quote": quote}]}], "uncertainties": []}


class ModalityVerifierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "memory.sqlite3")
        self.store.create_project("one", "One")
        self.original = "Баннер пришлю завтра до 12:00."
        r = row(text=self.original)
        r["speaker"] = "Клиент"
        self.store.import_rows([r], "one")
        self.allowed = {m["label"]: m for m in self.store.current("one")}
        self.good = ("Клиент обещал прислать баннер «завтра до 12:00» относительно даты "
                     "исходного сообщения; календарная дата неизвестна.")

    def tearDown(self):
        self.tmp.cleanup()

    def test_promise_is_typed_for_verifier(self):
        c = answer(self.good, self.original)["claims"][0]
        self.assertEqual(infer_claim_kind(c, self.allowed), "PROMISE")
        self.assertTrue(verifier_recheck_allowed("PROMISE"))
        self.assertFalse(verifier_recheck_allowed("FACT"))
        self.assertFalse(verifier_recheck_allowed("COMPLETION"))

    def test_rejected_promise_gets_one_specialized_recheck_and_can_be_rescued(self):
        c = ScriptedClient([
            ("answer", answer(self.good, self.original)),
            ("verify", verified("unsupported")),
            ("verify", verified("supported")),
        ])
        result = Service(self.store, ROOT, c).ask("one", "Когда клиент передаст баннер?")
        self.assertEqual(result["status"], "supported")
        self.assertEqual(result["claims"][0]["text"], self.good)
        self.assertEqual(result["dropped_claims"], 0)
        self.assertEqual(result["diagnostics"]["verifier_modality_rechecks"], 1)
        self.assertEqual([x["phase"] for x in result["diagnostics"]["steps"]],
                         ["draft", "verify_1", "verify_modality_1"])
        self.assertIn('"kind":"PROMISE"', c.calls[1]["user"])
        self.assertIn('"kind":"PROMISE"', c.calls[2]["user"])

    def test_uncertain_promise_can_be_rechecked_but_only_supported_recheck_rescues(self):
        c = ScriptedClient([
            ("answer", answer(self.good, self.original)),
            ("verify", verified("uncertain")),
            ("verify", verified("unsupported")),
        ])
        result = Service(self.store, ROOT, c).ask("one", "Когда клиент передаст баннер?")
        self.assertEqual(result["status"], "not_verified")
        self.assertEqual(result["claims"], [])
        self.assertEqual(result["dropped_claims"], 1)
        self.assertEqual(result["diagnostics"]["verifier_modality_rechecks"], 1)

    def test_generic_fact_rejection_is_not_retried(self):
        text = "Формат экспорта — JSON."
        self.store.import_rows([row(mid="m2", text=text)], "one")
        c = ScriptedClient([("answer", answer(text, text, "M2")),
                            ("verify", verified("unsupported"))])
        result = Service(self.store, ROOT, c).ask("one", "Какой формат экспорта?")
        self.assertEqual(result["status"], "not_verified")
        self.assertEqual(result["diagnostics"]["verifier_modality_rechecks"], 0)
        self.assertEqual(len(c.calls), 2)

    def test_completion_rejection_is_not_retried(self):
        done = "Баннер уже передан."
        self.store.import_rows([row(mid="m2", text=done)], "one")
        c = ScriptedClient([("answer", answer(done, done, "M2")),
                            ("verify", verified("unsupported"))])
        result = Service(self.store, ROOT, c).ask("one", "Баннер уже передан?")
        self.assertEqual(result["status"], "not_verified")
        self.assertEqual(result["diagnostics"]["verifier_modality_rechecks"], 0)
        self.assertEqual(len(c.calls), 2)

    def test_specialized_recheck_receives_full_context_and_never_current_date(self):
        c = ScriptedClient([
            ("answer", answer(self.good, self.original)),
            ("verify", verified("unsupported")),
            ("verify", verified("supported")),
        ])
        Service(self.store, ROOT, c).ask("one", "Когда клиент передаст баннер?")
        retry = c.calls[2]
        self.assertIn(self.original, retry["user"])
        self.assertIn("Клиент", retry["user"])
        self.assertNotIn("2026-09-08", retry["user"])
        self.assertIn("PROMISE", retry["system"])


class VersionTests(unittest.TestCase):
    def test_static_footer_and_package_version_match(self):
        import pm_app
        html = (ROOT / "pm_app/static/index.html").read_text(encoding="utf-8")
        self.assertEqual(pm_app.__version__, "1.0.0")
        self.assertIn('id="app-version">1.0.0', html)
        self.assertNotIn("v0.1.2", html)


if __name__ == "__main__":
    unittest.main()
