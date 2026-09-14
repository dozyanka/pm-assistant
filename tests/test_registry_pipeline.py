"""Verified project registry, manager review, history and safe migration."""
from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
import http.client
import threading
import time
from pathlib import Path

from pm_app import __version__
from pm_app.db import SCHEMA, Store
from pm_app.ollama import CHAT_MODEL, EMBED_MODEL, OllamaError
from pm_app.registry import validate_registry_extract
from pm_app.service import Service
from pm_app.web import AppState, LocalServer
from test_app import row

ROOT = Path(__file__).resolve().parents[1]


class RegistryClient:
    def __init__(self, extraction: dict, verdicts: list[str] | None = None):
        self.extraction = extraction
        self.verdicts = verdicts or ["supported"] * 30
        self.calls = []
        self.last_chat_stats = {}

    def models(self):
        return {CHAT_MODEL: "chat-registry-v1", EMBED_MODEL: "embed-v1"}

    def chat(self, system, user, schema, max_tokens=2048):
        self.calls.append({"system": system, "user": user, "schema": schema, "max_tokens": max_tokens})
        if "entries" in schema.get("properties", {}):
            return copy.deepcopy(self.extraction)
        if "checks" in schema.get("properties", {}):
            candidates = json.loads(user.split("\nCANDIDATES:\n", 1)[1])
            return {"checks": [{"claim_number": c["claim_number"], "verdict": self.verdicts[i]}
                               for i, c in enumerate(candidates)]}
        raise AssertionError("Unexpected model schema")


def decision(local_id: str, label: str, quote: str, statement: str, supersedes=None) -> dict:
    return {"local_id": local_id, "kind": "DECISION", "subject": "Доставка",
            "statement": statement, "lifecycle": "agreed", "responsible": "",
            "deadline_text": "", "deadline_iso": "", "evidence": [{"id": label, "quote": quote}],
            "supersedes": supersedes or []}


class MigrationTests(unittest.TestCase):
    def test_v1_database_is_migrated_without_losing_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.sqlite3"
            con = sqlite3.connect(path)
            con.executescript(SCHEMA)
            con.commit(); con.close()
            store = Store(path)
            store.create_project("one", "One")
            store.import_rows([row()], "one")
            con = sqlite3.connect(path)
            self.assertEqual(con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "4")
            self.assertIsNotNone(con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='registry_entries'").fetchone())
            con.close()
            self.assertEqual(len(store.current("one")), 1)


class RegistryValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "memory.sqlite3")
        self.store.create_project("one", "One")
        r = row(text="Клиент: баннер пришлю завтра до 12:00.")
        r["speaker"] = "Клиент"
        self.store.import_rows([r], "one")
        self.msg = self.store.current("one")[0]
        self.allowed = {self.msg["label"]: self.msg}

    def tearDown(self):
        self.tmp.cleanup()

    def test_invented_quote_rejects_registry_draft(self):
        item = {"entries": [{"local_id": "R1", "kind": "PROMISE", "subject": "Баннер",
            "statement": "Клиент обещал прислать баннер.", "lifecycle": "open", "responsible": "Клиент",
            "deadline_text": "завтра до 12:00", "deadline_iso": "", "evidence": [{"id": self.msg["label"], "quote": "выдуманная цитата"}], "supersedes": []}]}
        with self.assertRaisesRegex(OllamaError, "does not match"):
            validate_registry_extract(item, self.allowed)

    def test_unsupported_calendar_normalization_is_removed_not_fabricated(self):
        quote = self.msg["raw"]["text"]
        item = {"entries": [{"local_id": "R1", "kind": "PROMISE", "subject": "Баннер",
            "statement": "Клиент обещал прислать баннер.", "lifecycle": "open", "responsible": "Клиент",
            "deadline_text": "завтра до 12:00", "deadline_iso": "2026-09-09", "evidence": [{"id": self.msg["label"], "quote": quote}], "supersedes": []}]}
        entries, dropped = validate_registry_extract(item, self.allowed)
        self.assertEqual(entries[0]["deadline_iso"], "")
        self.assertEqual(dropped, 1)


class RegistryServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "memory.sqlite3")
        self.store.create_project("one", "One")
        self.q1 = "Оставляем на запуск только самовывоз."
        self.q2 = "Курьерскую доставку оставляем только для Москвы; самовывоз остаётся во всех регионах."
        self.store.import_rows([row(mid="m1", text=self.q1), row(mid="m2", text=self.q2)], "one")
        self.messages = self.store.current("one")
        self.labels = [m["label"] for m in self.messages]
        self.extraction = {"entries": [
            decision("R1", self.labels[0], self.q1, "На запуск согласован только самовывоз."),
            decision("R2", self.labels[1], self.q2, "Самовывоз остаётся во всех регионах, курьер — только Москва.", ["R1"]),
        ]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_verified_registry_is_persisted_with_supersedes_relation(self):
        client = RegistryClient(self.extraction)
        result = Service(self.store, ROOT, client).registry_sync("one")
        self.assertEqual((result["extracted"], result["accepted"], result["rejected"]), (2, 2, 0))
        reg = self.store.registry("one")
        self.assertEqual(reg["counts"]["candidate"], 2)
        newer = next(e for e in reg["entries"] if "курьер" in e["statement"].casefold())
        older = next(e for e in reg["entries"] if e["id"] != newer["id"])
        self.assertEqual(newer["supersedes"], [older["id"]])
        self.assertEqual(older["superseded_by"], [newer["id"]])
        self.assertTrue(all(e["evidence"] for e in reg["entries"]))

    def test_verifier_rejection_never_enters_review_queue(self):
        client = RegistryClient(self.extraction, ["supported", "unsupported"])
        result = Service(self.store, ROOT, client).registry_sync("one")
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(result["rejected"], 1)
        self.assertEqual(self.store.registry("one")["counts"]["total"], 1)

    def test_repeat_sync_is_idempotent(self):
        service = Service(self.store, ROOT, RegistryClient(self.extraction))
        service.registry_sync("one")
        ids1 = {e["stable_key"]: e["id"] for e in self.store.registry("one")["entries"]}
        service.registry_sync("one")
        ids2 = {e["stable_key"]: e["id"] for e in self.store.registry("one")["entries"]}
        self.assertEqual(ids1, ids2)
        self.assertEqual(len(ids2), 2)

    def test_manager_edit_survives_model_refresh_and_history_is_kept(self):
        service = Service(self.store, ROOT, RegistryClient(self.extraction))
        service.registry_sync("one")
        entry = next(e for e in self.store.registry("one")["entries"] if "курьер" in e["statement"].casefold())
        self.store.registry_action("one", entry["id"], "edit", {"statement": "Формулировка менеджера"})
        # Model proposes a different display wording for the same evidence-based stable key.
        changed = copy.deepcopy(self.extraction)
        target = next(x for x in changed["entries"] if self.labels[1] in [e["id"] for e in x["evidence"]])
        target["statement"] = "Другая формулировка модели"
        service2 = Service(self.store, ROOT, RegistryClient(changed))
        service2.registry_sync("one")
        after = next(e for e in self.store.registry("one")["entries"] if e["id"] == entry["id"])
        self.assertEqual(after["statement"], "Формулировка менеджера")
        self.assertEqual(after["review_status"], "edited")
        actions = [h["action"] for h in self.store.registry_history("one", entry["id"])]
        self.assertIn("edit", actions)

    def test_repeat_same_snapshot_does_not_mark_candidates_stale_from_model_variance(self):
        Service(self.store, ROOT, RegistryClient(self.extraction)).registry_sync("one")
        Service(self.store, ROOT, RegistryClient({"entries": []})).registry_sync("one")
        reg = self.store.registry("one")
        self.assertEqual(reg["counts"]["total"], 2)
        self.assertEqual(reg["counts"]["stale"], 0)

    def test_complete_is_manager_state_and_does_not_change_original_message(self):
        Service(self.store, ROOT, RegistryClient(self.extraction)).registry_sync("one")
        entry = self.store.registry("one")["entries"][0]
        original = [m["raw"]["text"] for m in self.store.current("one")]
        self.store.registry_action("one", entry["id"], "complete")
        after = next(e for e in self.store.registry("one")["entries"] if e["id"] == entry["id"])
        self.assertEqual(after["lifecycle"], "completed")
        self.assertEqual(after["origin"], "manager")
        self.assertEqual([m["raw"]["text"] for m in self.store.current("one")], original)

    def test_other_project_never_enters_registry_prompt(self):
        self.store.create_project("two", "Two")
        self.store.import_rows([row(pid="two", text="CROSS_PROJECT_SECRET")], "two")
        client = RegistryClient(self.extraction)
        Service(self.store, ROOT, client).registry_sync("one")
        self.assertTrue(client.calls)
        self.assertNotIn("CROSS_PROJECT_SECRET", "\n".join(c["user"] for c in client.calls))




class RegistryHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        placeholder = {"entries": []}
        cls.client = RegistryClient(placeholder)
        cls.state = AppState(ROOT, Path(cls.tmp.name), cls.client)
        cls.state.store.create_project("one", "One")
        text = "Нужно подготовить финальный отчёт."
        cls.state.store.import_rows([row(text=text)], "one")
        label = cls.state.store.current("one")[0]["label"]
        cls.client.extraction = {"entries": [{"local_id": "R1", "kind": "TASK",
            "subject": "Финальный отчёт", "statement": "Нужно подготовить финальный отчёт.",
            "lifecycle": "open", "responsible": "", "deadline_text": "", "deadline_iso": "",
            "evidence": [{"id": label, "quote": text}], "supersedes": []}]}
        cls.server = LocalServer(("127.0.0.1", 0), cls.state)
        cls.port = cls.server.server_port
        cls.origin = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(); cls.tmp.cleanup()

    def request(self, method, path, value=None):
        con = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": f"127.0.0.1:{self.port}"}
        body = None
        if value is not None:
            body = json.dumps(value).encode()
            headers.update({"Content-Type":"application/json", "Origin":self.origin, "X-PM-Token":self.state.token})
        con.request(method, path, body, headers)
        res=con.getresponse(); data=json.loads(res.read() or b"{}") if res.status != 204 else {}
        status=res.status; con.close(); return status,data

    def test_registry_sync_get_confirm_over_http(self):
        status,data=self.request("POST","/api/registry/sync",{"project_id":"one","topic_id":None})
        self.assertEqual(status,202)
        for _ in range(50):
            status,job=self.request("GET",f"/api/job?id={data['job_id']}")
            if job.get("state") != "running": break
            time.sleep(.02)
        self.assertEqual(job.get("state"),"done")
        status,reg=self.request("GET","/api/registry?project=one")
        self.assertEqual(status,200); self.assertEqual(reg["counts"]["candidate"],1)
        eid=reg["entries"][0]["id"]
        status,_=self.request("POST","/api/registry/action",{"project_id":"one","entry_id":eid,"action":"confirm"})
        self.assertEqual(status,200)
        _,reg=self.request("GET","/api/registry?project=one")
        self.assertEqual(reg["entries"][0]["review_status"],"confirmed")


class VersionTests(unittest.TestCase):
    def test_version_and_footer_match(self):
        html = (ROOT / "pm_app/static/index.html").read_text(encoding="utf-8")
        self.assertEqual(__version__, "1.0.0")
        self.assertIn('id="app-version">1.0.0', html)


if __name__ == "__main__":
    unittest.main()
