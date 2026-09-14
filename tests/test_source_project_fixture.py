"""Standalone source-project fixture and import-path regressions."""
from __future__ import annotations

import json
import tempfile
import unittest
import http.client
import threading
from collections import Counter
from pathlib import Path

from pm_app import __version__
from pm_app.db import Store
from pm_app.service import Service
from pm_app.web import AppState, LocalServer
from pm_app.source_examples import SOURCE_PROJECT_1_ID, load_source_project_1

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_SOURCE_FIXTURE = (ROOT / "samples" / "source_project_1" / "messages.jsonl").exists() and (ROOT / "samples" / "messages.jsonl").exists()


def _canonical_source_row(row: dict) -> dict:
    value = dict(row)
    value.pop("original_project_id", None)
    value.pop("original_project_name", None)
    value["project_id"] = "urbankey"
    return value


@unittest.skipUnless(PRIVATE_SOURCE_FIXTURE, "case-provided source-project fixture is not included in the public repository")
class SourceProject1FixtureTests(unittest.TestCase):
    def test_version(self):
        self.assertEqual(__version__, "1.0.0")

    def test_fixture_is_exactly_34_messages_and_four_sources(self):
        rows, manifest = load_source_project_1(ROOT)
        self.assertEqual(len(rows), 34)
        self.assertEqual(manifest["original_project_name"], "UrbanKey")
        self.assertEqual(len({r["source_id"] for r in rows}), 4)
        self.assertEqual(Counter(r["source_type"] for r in rows), {
            "client_chat": 15, "internal_chat": 4, "email": 1, "meeting_transcript": 14,
        })
        self.assertTrue(all(r.get("topic_id") is None for r in rows))

    def test_fixture_matches_original_urbankey_demo_rows_without_stress_mix(self):
        original = [json.loads(line) for line in (ROOT / "samples" / "messages.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        original = [r for r in original if r["project_id"] == "urbankey"]
        standalone, _ = load_source_project_1(ROOT)
        self.assertEqual([_canonical_source_row(r) for r in standalone], original)
        stress_text = (ROOT / "samples" / "stress_messages.jsonl").read_text(encoding="utf-8")
        standalone_texts = {r["text"] for r in standalone}
        for line in stress_text.splitlines():
            if not line.strip():
                continue
            stress = json.loads(line)
            self.assertNotIn(stress["text"], standalone_texts)

    def test_source_locators_still_point_to_project_1_tables(self):
        rows, _ = load_source_project_1(ROOT)
        self.assertEqual({r["source_locator"]["table"] for r in rows}, {1, 2, 3, 4})
        self.assertTrue(all("Синтетические матриалы" in r["source_locator"]["file"] for r in rows))


@unittest.skipUnless(PRIVATE_SOURCE_FIXTURE, "case-provided source-project fixture is not included in the public repository")
class SourceProject1ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "memory.sqlite3")
        self.service = Service(self.store, ROOT)

    def tearDown(self):
        self.tmp.cleanup()

    def test_import_creates_independent_named_project(self):
        result = self.service.source_project_1_import()
        self.assertEqual(result["project_id"], SOURCE_PROJECT_1_ID)
        self.assertEqual(result["message_count"], 34)
        project = self.store.require_project(SOURCE_PROJECT_1_ID)
        self.assertEqual(project["name"], "UrbanKey — исходный пример №1")
        current = self.store.current(SOURCE_PROJECT_1_ID)
        self.assertEqual(len(current), 34)
        self.assertEqual(len({m["raw"]["source_id"] for m in current}), 4)

    def test_import_is_idempotent_and_does_not_touch_normal_urbankey(self):
        self.service.demo_import()
        normal_before = [m["raw"] for m in self.store.current("urbankey")]
        first = self.service.source_project_1_import()
        second = self.service.source_project_1_import()
        self.assertEqual(first["added"], 34)
        self.assertEqual(second["added"], 0)
        self.assertEqual(second["unchanged"], 34)
        self.assertEqual([m["raw"] for m in self.store.current("urbankey")], normal_before)
        self.assertEqual(len(self.store.current(SOURCE_PROJECT_1_ID)), 34)


@unittest.skipUnless(PRIVATE_SOURCE_FIXTURE, "case-provided source-project fixture is not included in the public repository")
class SourceProject1HttpTests(unittest.TestCase):
    def test_http_import_button_route_creates_standalone_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = AppState(ROOT, Path(tmp))
            server = LocalServer(("127.0.0.1", 0), state)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_port
                body = b"{}"
                con = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                con.request("POST", "/api/import-source-project-1", body=body, headers={
                    "Host": f"127.0.0.1:{port}",
                    "Origin": f"http://127.0.0.1:{port}",
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)),
                    "X-PM-Token": state.token,
                })
                response = con.getresponse()
                payload = json.loads(response.read())
                con.close()
                self.assertEqual(response.status, 200)
                self.assertEqual(payload["project_id"], SOURCE_PROJECT_1_ID)
                self.assertEqual(payload["message_count"], 34)
                self.assertEqual(payload["source_count"], 4)
                self.assertEqual(len(state.store.current(SOURCE_PROJECT_1_ID)), 34)
            finally:
                server.shutdown(); server.server_close(); thread.join()


if __name__ == "__main__":
    unittest.main()
