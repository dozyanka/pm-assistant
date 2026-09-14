"""Large-scope Q&A splitting and source isolation regressions."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pm_app.db import Store
from pm_app.ollama import CHAT_MODEL, EMBED_MODEL
from pm_app.service import Service


ROOT = Path(__file__).resolve().parents[1]


def row(mid="m1", text="The meeting starts at 14:30.", pid="one", source="chat"):
    return {"project_id": pid, "source_id": source, "message_id": mid, "text": text,
            "source_type": "client_chat", "speaker": "PM", "occurred_at": None, "timezone": None}


class EchoAnswerClient:
    def __init__(self):
        self.chat_calls = []

    def models(self):
        return {CHAT_MODEL: "chat-v1", EMBED_MODEL: "embed-v1"}

    def chat(self, system, user, schema, max_tokens=1400):
        self.chat_calls.append((system, user, schema))
        if "checks" in schema.get("properties", {}):
            data = json.loads(user.split("\nCANDIDATES:\n", 1)[1])
            return {"checks": [{"claim_number": c["claim_number"], "verdict": "supported"} for c in data]}
        if "message_ids" in schema.get("properties", {}):
            return {"message_ids": []}
        marker_line = next(line for line in user.splitlines() if line.startswith('["M'))
        parsed = json.loads(marker_line)
        label, quote = parsed[0], parsed[5]
        return {"status": "supported", "claims": [{"text": "Marker found in selected source.", "evidence": [{"id": label, "quote": quote}]}], "uncertainties": []}


class OversizedAnswerSplitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db.sqlite3")
        self.store.create_project("one", "Project One")
        self.client = EchoAnswerClient()
        self.service = Service(self.store, ROOT, self.client)

    def tearDown(self):
        self.tmp.cleanup()

    def test_large_all_sources_scope_is_split_by_source_and_saved_once(self):
        rows = []
        for i in range(1, 8):
            for j in range(1, 4):
                rows.append(row(mid=f"m{i}-{j}", source=f"src-{i}", text=(f"SRC{i}_ONLY msg {j} " + ("x" * 340))))
        self.store.import_rows(rows, "one")

        result = self.service.ask("one", "What is agreed across the project?")

        self.assertEqual(result["grouping"], "source")
        self.assertEqual(result["verification"], "separate-complete-source-scopes")
        self.assertEqual(result["scope"]["message_count"], 21)
        self.assertEqual(len(result["groups"]), 7)
        self.assertEqual(sum(g["answer"]["scope"]["message_count"] for g in result["groups"]), 21)
        self.assertEqual(len(self.store.answers("one")), 1)

        tokens = [f"SRC{i}_ONLY" for i in range(1, 8)]
        for system, user, schema in self.client.chat_calls:
            matches = [token for token in tokens if token in user]
            self.assertEqual(len(matches), 1)

    def test_large_topic_scope_is_also_split_by_source(self):
        rows = []
        for i in range(1, 5):
            for j in range(1, 5):
                r = row(mid=f"m{i}-{j}", source=f"src-{i}", text=(f"TOPIC_SRC{i} msg {j} " + ("y" * 420)))
                r["topic_id"] = "urban"
                r["topic_name"] = "UrbanKey"
                rows.append(r)
        self.store.import_rows(rows, "one")

        result = self.service.ask("one", "What is agreed?", topic_id="urban")

        self.assertEqual(result["grouping"], "source")
        self.assertEqual(len(result["groups"]), 4)
        self.assertTrue(all(group["answer"]["scope"]["topic_id"] == "urban" for group in result["groups"]))


if __name__ == "__main__":
    unittest.main()
