"""Complete chunked registry extraction for large projects."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pm_app.db import Store
from pm_app.ollama import CHAT_MODEL, EMBED_MODEL
from pm_app.registry import merge_registry_candidates
from pm_app.service import Service, registry_chunks
from test_app import row

ROOT = Path(__file__).resolve().parents[1]


class ChunkRegistryClient:
    """Returns one evidence-backed promise whenever the marker row is visible."""
    def __init__(self):
        self.calls = []
        self.last_chat_stats = {}

    def models(self):
        return {CHAT_MODEL: "chat-chunk-v1", EMBED_MODEL: "embed-v1"}

    def chat(self, system, user, schema, max_tokens=2048):
        self.calls.append((system, user, schema, max_tokens))
        if "entries" in schema.get("properties", {}):
            marker = None
            for line in user.splitlines():
                if not line.startswith("["):
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if len(data) >= 6 and isinstance(data[5], str) and data[5].startswith("BANNER_PROMISE"):
                    marker = data
                    break
            if marker is None:
                return {"entries": []}
            return {"entries": [{
                "local_id": "R1", "kind": "PROMISE", "subject": "Баннер",
                "statement": "Клиент обещал передать баннер.", "lifecycle": "open",
                "responsible": "Клиент", "deadline_text": "завтра", "deadline_iso": "",
                "evidence": [{"id": marker[0], "quote": marker[5]}], "supersedes": []
            }]}
        if "checks" in schema.get("properties", {}):
            candidates = json.loads(user.split("\nCANDIDATES:\n", 1)[1])
            return {"checks": [{"claim_number": c["claim_number"], "verdict": "supported"}
                               for c in candidates]}
        raise AssertionError("Unexpected schema")


def registry_item(uid: str, revision: int, statement: str, *, deadline: str = "") -> dict:
    return {
        "local_id": "R1", "kind": "DECISION", "subject": "Доставка", "statement": statement,
        "lifecycle": "agreed", "responsible": "", "deadline_text": deadline, "deadline_iso": "",
        "evidence": [{"id": f"M{revision}", "quote": statement, "revision_id": revision}],
        "supersedes": [], "stable_key": f"k{revision}", "_uid": uid, "_supersedes_uids": []
    }


class RegistryChunkPlanningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db.sqlite3")
        self.store.create_project("one", "One")

    def tearDown(self):
        self.tmp.cleanup()

    def test_explicit_event_index_allows_cross_channel_windows_without_losing_rows(self):
        rows = []
        for i in range(1, 49):
            r = row(mid=f"m{i}", text=f"event {i}", source="a" if i % 2 else "b")
            r["source_locator"] = {"event_index": i}
            rows.append(r)
        self.store.import_rows(rows, "one")
        messages = self.store.current("one")
        windows = registry_chunks(messages)
        self.assertGreater(len(windows), 1)
        self.assertEqual({m["label"] for w in windows for m in w}, {m["label"] for m in messages})
        first_indexes = [m["raw"]["source_locator"]["event_index"] for m in windows[0]]
        self.assertEqual(first_indexes, sorted(first_indexes))
        self.assertGreater(len({m["source_id"] for m in windows[0]}), 1)

    def test_large_project_is_processed_in_chunks_and_overlap_duplicates_are_merged(self):
        rows = []
        for i in range(1, 61):
            text = "BANNER_PROMISE client will send banner tomorrow" if i == 22 else f"ordinary message {i}"
            r = row(mid=f"m{i}", text=text, source="chat")
            if i == 22:
                r["speaker"] = "Клиент"
            rows.append(r)
        self.store.import_rows(rows, "one")
        stages = []
        result = Service(self.store, ROOT, ChunkRegistryClient()).registry_sync("one", stages.append)
        self.assertTrue(result["coverage_complete"])
        self.assertEqual(result["processed_message_count"], 60)
        self.assertGreater(result["chunk_count"], 1)
        self.assertEqual(result["extracted"], 2)  # marker is in two overlapping windows
        self.assertEqual(result["verified_candidates"], 2)
        self.assertEqual(result["deduplicated"], 1)
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(self.store.registry("one")["counts"]["total"], 1)
        self.assertTrue(any(x.startswith("registry_chunk:") for x in stages))
        self.assertIn("registry_merge", stages)


class RegistryMergeTests(unittest.TestCase):
    def test_same_verified_fact_from_two_windows_is_deduplicated(self):
        left = registry_item("C1:R1", 10, "Самовывоз остаётся во всех регионах.")
        right = registry_item("C2:R1", 11, "Самовывоз остаётся во всех регионах.")
        merged, count = merge_registry_candidates([left, right])
        self.assertEqual(count, 1)
        self.assertEqual(len(merged), 1)
        self.assertEqual({e["revision_id"] for e in merged[0]["evidence"]}, {10, 11})

    def test_changed_agreements_with_same_subject_are_not_collapsed(self):
        old = registry_item("C1:R1", 10, "На запуск оставляем только самовывоз.")
        new = registry_item("C2:R1", 11, "Курьер остаётся для Москвы, самовывоз — для всех регионов.")
        merged, count = merge_registry_candidates([old, new])
        self.assertEqual(count, 0)
        self.assertEqual(len(merged), 2)


if __name__ == "__main__":
    unittest.main()
