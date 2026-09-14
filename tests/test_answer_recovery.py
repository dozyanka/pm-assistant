"""Regression tests use scripted replies, not real model-quality measurements."""
from __future__ import annotations
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from pm_app.db import Store
from pm_app.ollama import CHAT_MODEL, EMBED_MODEL, NUM_CTX, OllamaClient, OllamaError
from pm_app.service import (Service, ANSWER_SCHEMA, format_context, recall_windows,
                            validate_locations, locate_schema, candidate_context)
from pm_app.texts import SYSTEM_QA

ROOT = Path(__file__).resolve().parents[1]
EMPTY = {"status": "not_found", "claims": [], "uncertainties": []}


def row(mid="m1", text="Export format: JSON.", source="client", project="one"):
    return {"project_id": project, "source_id": source, "message_id": mid,
            "source_type": "client_chat", "speaker": "PM", "text": text,
            "occurred_at": None, "timezone": None}


def answer(text="Export format: JSON.", label="M1"):
    return {"status": "supported", "claims": [
        {"text": text, "evidence": [{"id": label, "quote": text}]}], "uncertainties": []}


def verified(*verdicts):
    return {"checks": [{"claim_number": i, "verdict": value}
                       for i, value in enumerate(verdicts, 1)]}


class ScriptedClient:
    def __init__(self, replies):
        self.replies = copy.deepcopy(replies)
        self.calls = []
        self.last_chat_stats = {"prompt_tokens": 123, "output_tokens": 12,
                                "done_reason": "stop", "thinking": "PRIVATE_INTERNAL",
                                "unexpected": "MUST_NOT_BE_COPIED"}

    def models(self):
        return {CHAT_MODEL: "test-chat", EMBED_MODEL: "test-embedding"}

    def chat(self, system, user, schema, max_tokens=2048):
        kind = ("locate" if "message_ids" in schema["properties"] else
                "verify" if "checks" in schema["properties"] else "answer")
        self.calls.append({"kind": kind, "system": system, "user": user, "schema": schema})
        if not self.replies:
            raise AssertionError("Unexpected extra model call: " + kind)
        expected, result = self.replies.pop(0)
        if expected != kind:
            raise AssertionError(f"Expected {expected}, got {kind}")
        if isinstance(result, Exception):
            raise result
        return copy.deepcopy(result)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "memory.sqlite3")
        self.store.create_project("one", "Private project name")
        self.store.create_project("two", "Other project")
        self.store.import_rows([row()], "one")

    def tearDown(self):
        self.tmp.cleanup()

    def run_case(self, replies, source=None, question="Which export format?"):
        client = ScriptedClient(replies)
        result = Service(self.store, ROOT, client).ask("one", question, source)
        self.assertFalse(client.replies, "Not all scripted steps were exercised")
        return result, client

    def recovery_plan(self, retry=None, verdict="supported"):
        return [
            ("answer", EMPTY), ("locate", {"message_ids": ["M1"]}),
            ("answer", answer() if retry is None else retry),
            ("verify", verified(verdict))
        ]

    def test_false_refusal_recovers_and_is_verified(self):
        result, client = self.run_case(self.recovery_plan())
        self.assertEqual(result["status"], "supported")
        self.assertEqual(result["claims"], answer()["claims"])
        self.assertEqual(result["verification"], "same-model-second-pass")
        self.assertEqual([c["kind"] for c in client.calls], ["answer", "locate", "answer", "verify"])
        self.assertTrue(result["diagnostics"]["recovery_used"])
        self.assertEqual(result["diagnostics"]["initial_status"], "not_found")

    def test_direct_supported_answer_keeps_two_pass_path(self):
        result, client = self.run_case([("answer", answer()), ("verify", verified("supported"))])
        self.assertEqual(result["status"], "supported")
        self.assertFalse(result["diagnostics"]["recovery_used"])
        self.assertEqual(len(client.calls), 2)

    def test_no_matching_evidence_stays_unknown(self):
        result, client = self.run_case([
            ("answer", EMPTY), ("locate", {"message_ids": []})], question="What is the deadline?")
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(result["claims"], [])
        self.assertEqual(result["verification"], "not-run")
        self.assertEqual(len(client.calls), 2)

    def test_second_empty_answer_returns_originals_not_a_fake_answer(self):
        result, client = self.run_case([
            ("answer", EMPTY), ("locate", {"message_ids": ["M1"]}), ("answer", EMPTY)])
        self.assertEqual(result["status"], "evidence_only")
        self.assertEqual(result["claims"], [])
        self.assertEqual(result["sources"], [])
        self.assertEqual(result["evidence_candidates"][0]["raw"]["text"], row()["text"])
        self.assertTrue(result["evidence_candidates"][0]["selected_by_lookup"])
        self.assertEqual(result["verification"], "not-run")
        self.assertEqual(len(client.calls), 3)

    def test_recovered_claim_is_not_published_when_verifier_rejects(self):
        result, _ = self.run_case(self.recovery_plan(verdict="unsupported"))
        self.assertEqual(result["status"], "not_verified")
        self.assertEqual(result["claims"], [])
        self.assertEqual(result["dropped_claims"], 1)
        self.assertEqual(result["verification"], "same-model-second-pass")

    def test_uncertain_verdict_is_not_overridden(self):
        result, _ = self.run_case(self.recovery_plan(verdict="uncertain"))
        self.assertEqual(result["status"], "not_verified")
        self.assertEqual(result["diagnostics"]["steps"][-1]["verdicts"], ["uncertain"])

    def test_recovery_does_not_hide_unselected_contradictions(self):
        self.store.import_rows([row("m2", "Export format: XML.")], "one")
        revised = {"status": "conflict", "claims": answer()["claims"] + answer(
            "Export format: XML.", "M2")["claims"], "uncertainties": ["No confirmed replacement."]}
        plan = self.recovery_plan(retry=revised)
        plan[-1] = ("verify", verified("supported", "supported"))
        plan.append(("verify", verified("supported")))
        result, client = self.run_case(plan)
        self.assertEqual(result["status"], "conflict")
        for call in client.calls:
            if call["kind"] in ("answer", "verify"):
                self.assertIn("Export format: XML.", call["user"])
        self.assertEqual(len(result["sources"]), 2)

    def test_recovery_never_crosses_selected_source_or_project(self):
        self.store.import_rows([row("s", "SECRET_OTHER_SOURCE", source="internal")], "one")
        self.store.import_rows([row("s", "SECRET_OTHER_PROJECT", project="two")], "two")
        result, client = self.run_case(self.recovery_plan(), source="client")
        self.assertEqual(result["scope"]["message_count"], 1)
        for call in client.calls:
            blob = json.dumps(call)
            self.assertNotIn("SECRET_OTHER_SOURCE", blob)
            self.assertNotIn("SECRET_OTHER_PROJECT", blob)

    def test_multiple_sources_are_all_scanned_after_empty_answer(self):
        self.store.import_rows([row("m2", "A second source.", source="internal")], "one")
        result, client = self.run_case([
            ("answer", EMPTY), ("locate", {"message_ids": []}),
            ("locate", {"message_ids": []})])
        self.assertEqual(result["diagnostics"]["recovery_windows"], 2)
        self.assertIn("A second source.", client.calls[-1]["user"])
        self.assertEqual(result["status"], "not_found")

    def test_lookup_cannot_cite_another_window(self):
        self.store.import_rows([row("m2", "A second source.", source="internal")], "one")
        client = ScriptedClient([("answer", EMPTY), ("locate", {"message_ids": ["M2"]})])
        with self.assertRaisesRegex(OllamaError, "outside"):
            Service(self.store, ROOT, client).ask("one", "Question")
        self.assertEqual(self.store.answers("one"), [])

    def test_malformed_lookup_remains_an_error(self):
        client = ScriptedClient([("answer", EMPTY), ("locate", {"bad": []})])
        with self.assertRaises(OllamaError):
            Service(self.store, ROOT, client).ask("one", "Question")
        self.assertEqual(self.store.answers("one"), [])

    def test_network_failure_does_not_become_not_found(self):
        client = ScriptedClient([("answer", EMPTY), ("locate", OllamaError("Local request failed"))])
        with self.assertRaisesRegex(OllamaError, "Local request"):
            Service(self.store, ROOT, client).ask("one", "Question")
        self.assertEqual(self.store.answers("one"), [])

    def test_fabricated_quote_in_retry_is_rejected(self):
        wrong = answer("Made-up deadline tomorrow.")
        client = ScriptedClient([
            ("answer", EMPTY), ("locate", {"message_ids": ["M1"]}), ("answer", wrong)])
        with self.assertRaisesRegex(OllamaError, "does not match"):
            Service(self.store, ROOT, client).ask("one", "Question")
        self.assertEqual(self.store.answers("one"), [])

    def test_invalid_json_fields_do_not_trigger_answer_forcing(self):
        client = ScriptedClient([("answer", {"bad": "output"})])
        with self.assertRaises(OllamaError):
            Service(self.store, ROOT, client).ask("one", "Question")
        self.assertEqual(len(client.calls), 1)

    def test_diagnostics_contains_no_source_text_question_or_project(self):
        result, _ = self.run_case(self.recovery_plan(), question="VERY_PRIVATE_QUESTION")
        metadata = json.dumps(result["diagnostics"])
        for secret in ("Export format", "Private project name", "VERY_PRIVATE_QUESTION",
                       "PRIVATE_INTERNAL", "MUST_NOT_BE_COPIED"):
            self.assertNotIn(secret, metadata)
        self.assertEqual(result["diagnostics"]["steps"][0]["prompt_tokens"], 123)
        self.assertEqual(result["diagnostics"]["pipeline_version"], "1.0.0")

    def test_answer_history_persists_diagnostics_without_becoming_source(self):
        result, _ = self.run_case(self.recovery_plan())
        saved = self.store.answers("one")[0]["response"]
        self.assertEqual(saved["diagnostics"], result["diagnostics"])
        self.assertEqual(len(self.store.current("one")), 1)

    def test_fallback_shows_neighbours_but_not_nearby_other_source(self):
        self.store.import_rows([
            row("m2", "Confirmed."),
            row("m3", "Irrelevant.", source="internal")], "one")
        msgs = self.store.current("one")
        cards = candidate_context(msgs, {"M2"})
        self.assertEqual([c["id"] for c in cards], ["M1", "M2"])
        self.assertFalse(cards[0]["selected_by_lookup"])
        self.assertTrue(cards[1]["selected_by_lookup"])

    def test_all_originals_are_covered_in_overlapping_windows(self):
        self.store.import_rows([row("long"+str(i), str(i) + " details " * 20) for i in range(30)], "one")
        messages = self.store.current("one")
        windows = recall_windows(messages)
        self.assertEqual({m["label"] for w in windows for m in w}, {m["label"] for m in messages})
        for first, second in zip(messages, messages[1:]):
            self.assertTrue(any(first in w and second in w for w in windows))
        self.assertTrue(all(len(w) <= 12 for w in windows))

    def test_lookup_windows_do_not_cross_source_boundaries(self):
        self.store.import_rows([row("m2", "Second source.", source="internal")], "one")
        self.assertTrue(all(len({m["source_id"] for m in w}) == 1
                            for w in recall_windows(self.store.current("one"))))

    def test_location_schema_restricts_allowed_ids(self):
        schema = locate_schema(["M1", "M4"])
        self.assertEqual(schema["properties"]["message_ids"]["items"]["enum"], ["M1", "M4"])
        for bad in (None, {"message_ids": "M1"}, {"message_ids": ["M1", "M1"]},
                    {"message_ids": ["M2"]}, {"message_ids": [1]}):
            with self.assertRaises(OllamaError):
                validate_locations(bad, {"M1"})

    def test_undated_sources_are_preserved_not_fabricated(self):
        result, _ = self.run_case(self.recovery_plan())
        self.assertIsNone(result["sources"][0]["raw"]["occurred_at"])
        self.assertTrue(result["scope"]["date_warning"])


class ClientGroundingTests(unittest.TestCase):
    def response(self, **extra):
        return {"message": {"content": json.dumps(EMPTY)}, "done": True,
                "done_reason": "stop", "prompt_eval_count": 123, "eval_count": 30, **extra}

    def test_schema_is_in_prompt_and_format_without_remote_tools(self):
        with patch.object(OllamaClient, "request", return_value=self.response()) as request:
            client = OllamaClient()
            self.assertEqual(client.chat(SYSTEM_QA, "Question and sources", ANSWER_SCHEMA), EMPTY)
        args = request.call_args.args
        self.assertEqual(args[0], "/api/chat")
        payload = args[1]
        self.assertEqual(payload["format"], ANSWER_SCHEMA)
        self.assertIn("JSON_SCHEMA:", payload["messages"][0]["content"])
        self.assertIn('"claims"', payload["messages"][0]["content"])
        self.assertFalse(payload["think"])
        self.assertEqual(payload["options"]["num_ctx"], 8192)
        self.assertNotIn("tools", payload)
        self.assertEqual(client.last_chat_stats["output_tokens"], 30)

    def test_length_limit_is_error_even_for_valid_empty_json(self):
        with patch.object(OllamaClient, "request", return_value=self.response(done_reason="length")):
            with self.assertRaisesRegex(OllamaError, "output limit"):
                OllamaClient().chat("Rules", "Sources", ANSWER_SCHEMA)

    def test_context_limit_is_not_missing_data(self):
        with patch.object(OllamaClient, "request", return_value=self.response(prompt_eval_count=NUM_CTX-10)):
            with self.assertRaisesRegex(OllamaError, "context limit"):
                OllamaClient().chat("Rules", "Sources", ANSWER_SCHEMA)

    def test_incomplete_response_is_rejected(self):
        with patch.object(OllamaClient, "request", return_value=self.response(done=False)):
            with self.assertRaisesRegex(OllamaError, "incomplete"):
                OllamaClient().chat("Rules", "Sources", ANSWER_SCHEMA)

    def test_schema_counts_towards_input_guard(self):
        with patch.object(OllamaClient, "request") as request:
            with self.assertRaisesRegex(OllamaError, "too large"):
                OllamaClient().chat("Rules", "S" * 11499, ANSWER_SCHEMA)
            request.assert_not_called()

    def test_no_example_answer_is_hardcoded_in_runtime(self):
        # Concrete delivery assertions belong to fixtures/evaluation, not routing rules.
        for name in ("service.py", "ollama.py", "texts.py"):
            source = (ROOT / "pm_app" / name).read_text(encoding="utf-8")
            self.assertNotIn("RetailFlow", source)
            self.assertNotIn("курьер", source)
            self.assertNotIn("самовывоз", source)


if __name__ == "__main__":
    unittest.main()
