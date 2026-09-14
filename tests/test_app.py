"""Deterministic tests with fake model outputs. Not a model-quality evaluation."""
from __future__ import annotations
import copy
import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from pm_app.db import Store, parse_jsonl, validate_row
from pm_app.ollama import CHAT_MODEL, EMBED_MODEL, OllamaClient, OllamaError, normalize_vector
from pm_app.service import Service, format_context, make_chunks, validate_answer, verifier_verdicts
from pm_app.web import AppState, LocalServer

ROOT = Path(__file__).resolve().parents[1]


def row(mid="m1", text="The meeting starts at 14:30.", pid="one", source="chat"):
    return {"project_id": pid, "source_id": source, "message_id": mid, "text": text,
            "source_type": "client_chat", "speaker": "PM", "occurred_at": None, "timezone": None}


class FakeClient:
    def __init__(self):
        self.chat_calls = []
        self.embed_calls = []
        self.answer = None
        self.verdict = "supported"
        self.model_digest = "embed-v1"
        self.embed_fail = False

    def models(self):
        return {CHAT_MODEL: "chat-v1", EMBED_MODEL: self.model_digest}

    def embed(self, texts, unload=False):
        self.embed_calls.append(texts)
        if self.embed_fail:
            raise OllamaError("fake embedding failure")
        return [normalize_vector([1.0, float(len(t) % 7 + 1)]) for t in texts]

    def chat(self, system, user, schema, max_tokens=1400):
        self.chat_calls.append((system, user, schema))
        if "message_ids" in schema["properties"]:
            return {"message_ids": []}
        if "checks" in schema["properties"]:
            data = json.loads(user.split("\nCANDIDATES:\n")[1])
            return {"checks": [{"claim_number": c["claim_number"], "verdict": self.verdict} for c in data]}
        if self.answer is not None:
            return copy.deepcopy(self.answer)
        return {"status": "supported", "claims": [{"text": "The meeting starts at 14:30.",
                "evidence": [{"id": "M1", "quote": "The meeting starts at 14:30."}]}], "uncertainties": []}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "memory.sqlite3")
        self.store.create_project("one", "Project One")
        self.store.create_project("two", "Project Two")

    def tearDown(self):
        self.temp.cleanup()

    def test_import_and_restart(self):
        original = row()
        self.store.import_rows([original], "one")
        reopened = Store(self.store.path)
        self.assertEqual(reopened.current("one")[0]["raw"], original)

    def test_idempotent_import(self):
        self.store.import_rows([row()], "one")
        result = self.store.import_rows([row()], "one")
        self.assertEqual(result, {"added": 0, "unchanged": 1, "revised": 0})
        self.assertEqual(len(self.store.current("one")), 1)

    def test_projects_are_separate_even_with_same_ids(self):
        self.store.import_rows([row()], "one")
        self.store.import_rows([row(pid="two", text="Other project secret")], "two")
        self.assertNotIn("Other project secret", json.dumps(self.store.current("one")))
        self.assertEqual(len(self.store.current("two")), 1)

    def test_source_scope(self):
        self.store.import_rows([row(source="chat-a"), row(source="chat-b")], "one")
        self.assertEqual(len(self.store.current("one", "chat-a")), 1)
        self.assertEqual(self.store.current("one", "missing"), [])

    def test_cross_project_import_rejected(self):
        with self.assertRaisesRegex(ValueError, "different project"):
            self.store.import_rows([row(), row(pid="two")], "one")
        self.assertEqual(self.store.current("one"), [])

    def test_revision_requires_explicit_permission(self):
        self.store.import_rows([row()], "one")
        with self.assertRaisesRegex(ValueError, "explicitly enable revisions"):
            self.store.import_rows([row(text="Changed")], "one")
        self.assertEqual(self.store.current("one")[0]["revision"], 1)

    def test_revisions_preserve_original(self):
        self.store.import_rows([row()], "one")
        self.store.import_rows([row(text="Changed")], "one", allow_revisions=True)
        current = self.store.current("one")[0]
        history = self.store.revision_history("one", current["message_fk"])
        self.assertEqual(current["raw"]["text"], "Changed")
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["raw"]["text"], row()["text"])
        self.assertEqual(self.store.revision_history("two", current["message_fk"]), [])

    def test_import_is_atomic_on_conflict(self):
        self.store.import_rows([row()], "one")
        with self.assertRaises(ValueError):
            self.store.import_rows([row(mid="new"), row(text="Changed")], "one")
        self.assertEqual(len(self.store.current("one")), 1)

    def test_conflicting_duplicate_in_one_file(self):
        with self.assertRaisesRegex(ValueError, "two different versions"):
            self.store.import_rows([row(), row(text="Other")], "one", allow_revisions=True)
        self.assertEqual(self.store.current("one"), [])

    def test_import_does_not_invent_date(self):
        r = row()
        r.update(clock_time="09:14", offset_text="00:22", offset_seconds=22)
        self.store.import_rows([r], "one")
        raw = self.store.current("one")[0]["raw"]
        self.assertIsNone(raw["occurred_at"])
        self.assertEqual(raw["offset_seconds"], 22)
        self.assertEqual(raw["clock_time"], "09:14")

    def test_full_source_metadata_preserved(self):
        r = row(); r["source_locator"] = {"file": "original.docx", "table": 4, "row": 9}
        r["custom_field"] = "retained"
        self.store.import_rows([r], "one")
        self.assertEqual(self.store.current("one")[0]["raw"], r)

    def test_naive_timestamp_rejected(self):
        r = row(); r["occurred_at"] = "2026-09-08T09:00:00"
        with self.assertRaisesRegex(ValueError, "UTC offset"):
            validate_row(r)

    def test_aware_timestamp_allowed(self):
        r = row(); r["occurred_at"] = "2026-09-08T09:00:00+05:00"
        validate_row(r)

    def test_jsonl_bom_blank_lines(self):
        text = "\ufeff\n" + json.dumps(row()) + "\n\n"
        self.assertEqual(parse_jsonl(text), [row()])

    def test_bad_jsonl_reports_line(self):
        with self.assertRaisesRegex(ValueError, "line 2"):
            parse_jsonl(json.dumps(row()) + "\nnot-json")

    def test_sql_injection_project_id_rejected(self):
        with self.assertRaises(ValueError):
            self.store.create_project("x'; DROP TABLE projects;--", "bad")
        self.assertEqual(len(self.store.projects()), 2)

    @unittest.skipUnless((ROOT / "samples" / "messages.jsonl").exists(), "case-provided starter fixture is not included in the public repository")
    def test_demo_count_and_separation(self):
        service = Service(self.store, ROOT, FakeClient())
        self.assertEqual(service.demo_import()["added"], 93)
        counts = {p["id"]: p["message_count"] for p in self.store.projects()}
        self.assertEqual([counts[k] for k in ["urbankey", "retailflow", "industrypulse"]], [34, 28, 31])
        self.assertEqual(service.demo_import()["unchanged"], 93)
        for p in ["urbankey", "retailflow", "industrypulse"]:
            self.assertNotIn("stress-fixture", json.dumps(self.store.current(p)))


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "memory.sqlite3")
        self.store.create_project("one", "One")
        self.store.create_project("two", "Two")
        self.store.import_rows([row()], "one")
        self.client = FakeClient()
        self.service = Service(self.store, ROOT, self.client)

    def tearDown(self):
        self.temp.cleanup()

    def test_quote_must_exist(self):
        data = self.client.chat("", "", {"properties": {}}, 10)
        data["claims"][0]["evidence"][0]["quote"] = "Invented deadline"
        with self.assertRaisesRegex(OllamaError, "does not match"):
            validate_answer(data, {m["label"]: m for m in self.store.current("one")})

    def test_citation_must_be_in_scope(self):
        data = self.client.chat("", "", {"properties": {}}, 10)
        data["claims"][0]["evidence"][0]["id"] = "M999"
        with self.assertRaisesRegex(OllamaError, "outside"):
            validate_answer(data, {m["label"]: m for m in self.store.current("one")})

    def test_answer_has_second_pass_and_is_saved(self):
        result = self.service.ask("one", "When is the meeting?")
        self.assertEqual(len(self.client.chat_calls), 2)
        self.assertEqual(result["status"], "supported")
        self.assertEqual(len(self.store.answers("one")), 1)
        self.assertEqual(self.store.answers("two"), [])
        self.assertTrue(result["scope"]["date_warning"])

    def test_other_project_never_enters_prompt(self):
        self.store.import_rows([row(pid="two", text="CROSS_PROJECT_SECRET")], "two")
        self.service.ask("one", "When is the meeting?")
        for system, user, schema in self.client.chat_calls:
            self.assertNotIn("CROSS_PROJECT_SECRET", user)

    def test_verifier_rejection_drops_claim(self):
        self.client.verdict = "unsupported"
        result = self.service.ask("one", "When is the meeting?")
        self.assertEqual(result["status"], "not_verified")
        self.assertEqual(result["claims"], [])
        self.assertEqual(result["sources"], [])

    def test_unknown_answer_stays_unknown(self):
        self.client.answer = {"status": "not_found", "claims": [], "uncertainties": []}
        result = self.service.ask("one", "What is the deadline?")
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(len(self.client.chat_calls), 2)
        self.assertTrue(result["diagnostics"]["recovery_used"])
        self.assertEqual(result["verification"], "not-run")

    def test_model_failure_is_not_not_found(self):
        self.client.answer = {"bad": "output"}
        with self.assertRaises(OllamaError):
            self.service.ask("one", "Question")
        self.assertEqual(self.store.answers("one"), [])

    def test_no_generated_answer_is_reingested(self):
        self.service.ask("one", "Question")
        self.assertEqual(len(self.store.current("one")), 1)
        self.assertEqual(self.store.current("one")[0]["raw"]["text"], row()["text"])

    def test_oversized_context_is_rejected_not_truncated(self):
        self.store.import_rows([row(mid="long", text="X" * 7000)], "one")
        with self.assertRaisesRegex(ValueError, "too large"):
            self.service.ask("one", "Question")
        self.assertEqual(self.client.chat_calls, [])

    def test_verifier_must_cover_every_claim(self):
        with self.assertRaisesRegex(OllamaError, "every claim"):
            verifier_verdicts({"checks": [{"claim_number": 1, "verdict": "supported"}]}, 2)

    def test_duplicate_verifier_number_rejected(self):
        check = {"claim_number": 1, "verdict": "supported"}
        with self.assertRaises(OllamaError):
            verifier_verdicts({"checks": [check, check]}, 2)

    def test_search_index_cached(self):
        first = self.service.search("one", "meeting")
        self.assertTrue(first["hits"])
        before = len(self.client.embed_calls)
        self.service.search("one", "another query")
        self.assertEqual(len(self.client.embed_calls), before + 1)

    def test_index_invalidated_by_new_messages(self):
        self.service.search("one", "meeting")
        self.store.import_rows([row(mid="m2", text="New message")], "one")
        self.assertIsNone(self.store.index("one")[0])

    def test_index_invalidated_by_model_digest(self):
        self.service.search("one", "meeting")
        self.client.model_digest = "embed-v2"
        before = len(self.client.embed_calls)
        self.service.search("one", "meeting")
        self.assertEqual(len(self.client.embed_calls), before + 2)

    def test_failed_embedding_does_not_save_partial_index(self):
        self.client.embed_fail = True
        with self.assertRaises(OllamaError):
            self.service.search("one", "meeting")
        self.assertIsNone(self.store.index("one")[0])

    def test_search_is_project_scoped(self):
        self.store.import_rows([row(pid="two", text="CROSS_PROJECT_SECRET")], "two")
        result = self.service.search("one", "meeting")
        self.assertNotIn("CROSS_PROJECT_SECRET", json.dumps(result))
        self.assertNotIn("CROSS_PROJECT_SECRET", str(self.client.embed_calls))

    def test_chunks_preserve_short_reply_and_source_boundaries(self):
        self.store.import_rows([row(mid="m2", text="Yes."), row(mid="m3", source="separate", text="Second source")], "one")
        messages = self.store.current("one")
        by_id = {m["id"]: m for m in messages}
        chunks = make_chunks(messages)
        self.assertIn("Yes.", chunks[0]["text"])
        for c in chunks:
            self.assertEqual(len({by_id[i]["source_id"] for i in c["revision_ids"]}), 1)

    @unittest.skipUnless((ROOT / "samples" / "messages.jsonl").exists(), "case-provided starter fixture is not included in the public repository")
    def test_demo_contexts_fit(self):
        self.service.demo_import()
        for pid in ("urbankey", "retailflow", "industrypulse"):
            self.assertLessEqual(len(format_context(self.store.current(pid))), 6400)

    def test_vector_validation(self):
        for vec in ([float("nan")], [0, 0], [True, 1], []):
            with self.assertRaises(OllamaError):
                normalize_vector(vec)
        self.assertAlmostEqual(sum(x*x for x in normalize_vector([3, 4])), 1.0)

    def test_no_arbitrary_ollama_paths(self):
        with self.assertRaises(OllamaError):
            OllamaClient().request("https://example.com/steal")

    def test_no_automatic_model_download(self):
        with patch.object(OllamaClient, "request", return_value={"models": []}) as request:
            with self.assertRaisesRegex(OllamaError, "No automatic download"):
                OllamaClient().models()
            self.assertEqual(request.call_count, 1)


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.state = AppState(ROOT, Path(cls.temp.name), FakeClient())
        cls.server = LocalServer(("127.0.0.1", 0), cls.state)
        cls.port = cls.server.server_port
        cls.origin = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join()
        cls.temp.cleanup()

    def request(self, method, path, value=None, headers=None):
        con = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        body = None if value is None else json.dumps(value).encode()
        con.request(method, path, body, headers or {})
        r = con.getresponse()
        status, hdr, data = r.status, dict(r.getheaders()), r.read()
        con.close()
        return status, hdr, data

    def test_homepage_and_security_headers(self):
        status, headers, data = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"PM Assistant", data)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("connect-src 'self'", headers["Content-Security-Policy"])

    def test_no_directory_or_database_serving(self):
        for path in ("/app_data/project_memory.sqlite3", "/../app.py", "/pm_app/db.py"):
            self.assertEqual(self.request("GET", path)[0], 404)

    def test_reject_wrong_host(self):
        self.assertEqual(self.request("GET", "/api/state", headers={"Host": "evil.example"})[0], 403)

    def test_reject_cross_site_get(self):
        self.assertEqual(self.request("GET", "/api/state", headers={"Sec-Fetch-Site": "cross-site"})[0], 403)

    def test_reject_write_without_csrf(self):
        headers = {"Content-Type": "application/json", "Origin": self.origin}
        self.assertEqual(self.request("POST", "/api/import-demo", {}, headers)[0], 403)

    def test_reject_write_from_other_origin(self):
        headers = {"Content-Type": "application/json", "Origin": "https://evil.example", "X-PM-Token": self.state.token}
        self.assertEqual(self.request("POST", "/api/import-demo", {}, headers)[0], 403)

    @unittest.skipUnless((ROOT / "samples" / "messages.jsonl").exists(), "case-provided starter fixture is not included in the public repository")
    def test_import_via_http(self):
        headers = {"Content-Type": "application/json", "Origin": self.origin, "X-PM-Token": self.state.token}
        status, _, data = self.request("POST", "/api/import-demo", {}, headers)
        self.assertEqual(status, 200)
        self.assertIn("added", json.loads(data))
        status, _, data = self.request("GET", "/api/messages?project=retailflow")
        self.assertEqual(len(json.loads(data)["messages"]), 28)

    def test_body_limit(self):
        headers = {"Content-Type": "application/json", "Origin": self.origin,
                   "X-PM-Token": self.state.token, "Content-Length": "99999999"}
        self.assertEqual(self.request("POST", "/api/import-demo", {}, headers)[0], 413)

    def test_lan_binding_prohibited(self):
        with self.assertRaises(ValueError):
            LocalServer(("0.0.0.0", 0), self.state)


if __name__ == "__main__":
    unittest.main()
