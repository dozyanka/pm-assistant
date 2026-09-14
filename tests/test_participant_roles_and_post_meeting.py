"""Participant roles, post-meeting safety and registry coverage regressions."""
from __future__ import annotations

import json
import sqlite3
import tempfile
from contextlib import closing
import unittest
from pathlib import Path

from pm_app import __version__
from pm_app.db import Store
from pm_app.ollama import CHAT_MODEL, EMBED_MODEL
from pm_app.registry import SYSTEM_REGISTRY_EXTRACT
from pm_app.service import Service, merge_post_meeting

ROOT = Path(__file__).resolve().parents[1]


def row(mid: str, speaker: str, text: str, source: str = "chat") -> dict:
    return {
        "project_id": "one", "source_id": source, "message_id": mid,
        "source_type": "unspecified", "speaker": speaker, "text": text,
        "occurred_at": None, "timezone": None,
    }


class RoleClient:
    def __init__(self):
        self.calls = []
        self.last_chat_stats = {}

    def models(self):
        return {CHAT_MODEL: "chat-role-v1", EMBED_MODEL: "embed-v1"}

    def chat(self, system, user, schema, max_tokens=2048):
        self.calls.append((system, user, schema, max_tokens))
        if "participants" in schema.get("properties", {}):
            speakers = schema["properties"]["participants"]["items"]["properties"]["speaker"]["enum"]
            result = []
            for speaker in speakers:
                if speaker == "Мария":
                    result.append({"speaker": speaker, "side": "client", "confidence": 88})
                else:
                    result.append({"speaker": speaker, "side": "unknown", "confidence": 35})
            return {"participants": result}
        raise AssertionError("Unexpected schema")


class FalseActionPostMeetingClient:
    def __init__(self):
        self.last_chat_stats = {}

    def models(self):
        return {CHAT_MODEL: "chat-post-v1", EMBED_MODEL: "embed-v1"}

    def chat(self, system, user, schema, max_tokens=2048):
        props = schema.get("properties", {})
        if set(props) == {"key_results", "client_actions", "our_actions", "fixed", "clarify"}:
            rows = {}
            for line in user.splitlines():
                if line.startswith('["M'):
                    data = json.loads(line)
                    rows[data[0]] = data[5]
            return {
                "key_results": [],
                "client_actions": [
                    {"text": "Подтверждаем получение материалов и доступов в согласованные сроки.",
                     "evidence": [{"id": "M1", "quote": rows["M1"]}, {"id": "M2", "quote": rows["M2"]}]},
                    {"text": "API-документацию и тестовые доступы передадим до пятницы.",
                     "evidence": [{"id": "M3", "quote": rows["M3"]}]},
                ],
                "our_actions": [], "fixed": [], "clarify": [],
            }
        if "checks" in props:
            candidates = json.loads(user.split("\nCANDIDATES:\n", 1)[1])
            return {"checks": [{"claim_number": c["claim_number"], "verdict": "supported"} for c in candidates]}
        raise AssertionError("Unexpected schema")


class ParticipantRolesAndPostMeetingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db.sqlite3")
        self.store.create_project("one", "One")

    def tearDown(self):
        self.tmp.cleanup()

    def test_version_and_participant_ui(self):
        self.assertEqual(__version__, "1.0.0")
        html = (ROOT / "pm_app" / "static" / "index.html").read_text(encoding="utf-8")
        js = "\n".join(path.read_text(encoding="utf-8") for path in sorted((ROOT / "pm_app" / "static" / "js").glob("*.js")))
        self.assertIn('id="participants-list"', html)
        self.assertIn('id="participants-infer"', html)
        self.assertIn('/api/participants/infer', js)
        self.assertIn('entry.kind==="DEPENDENCY"', js)

    def test_schema_v3_upgrades_to_v4_with_participant_table(self):
        db = self.store.path
        with closing(sqlite3.connect(db)) as con, con:
            con.execute("DROP TABLE participant_roles")
            con.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
        reopened = Store(db)
        with closing(reopened.connect()) as con:
            self.assertEqual(con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "4")
            self.assertIsNotNone(con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='participant_roles'").fetchone())

    def test_roles_are_separate_from_original_message_json(self):
        original = row("m1", "Мария", "Со своей стороны пришлём материалы завтра.")
        self.store.import_rows([original], "one")
        before = self.store.current("one")[0]["raw"]
        self.store.set_participant_role("one", "Мария", "client")
        after = self.store.current("one")[0]["raw"]
        self.assertEqual(before, original)
        self.assertEqual(after, original)
        role = self.store.participant_role_map("one", trusted_only=True)["Мария"]
        self.assertEqual(role["side"], "client")
        self.assertTrue(role["confirmed"])

    def test_explicit_labels_are_trusted_without_changing_json(self):
        self.store.import_rows([
            row("m1", "Клиент", "Передадим материалы."),
            row("m2", "PM Xpage", "После этого проверим."),
            row("m3", "Алексей, Xpage", "Возьму проверку на себя."),
        ], "one")
        roles = self.store.participant_role_map("one", trusted_only=True)
        self.assertEqual(roles["Клиент"]["side"], "client")
        self.assertEqual(roles["PM Xpage"]["side"], "our_team")
        self.assertEqual(roles["Алексей, Xpage"]["side"], "our_team")

    def test_ai_role_is_suggestion_until_pm_confirms_and_cannot_override_manager(self):
        self.store.import_rows([
            row("m1", "Мария", "Со своей стороны документы пришлём завтра."),
            row("m2", "Иван", "После получения начнём работу."),
        ], "one")
        service = Service(self.store, ROOT, RoleClient())
        result = service.infer_participant_roles("one")
        maria = next(p for p in result["participants"] if p["speaker"] == "Мария")
        self.assertEqual(maria["side"], "client")
        self.assertEqual(maria["source"], "ai")
        self.assertFalse(maria["confirmed"])
        self.assertNotIn("Мария", self.store.participant_role_map("one", trusted_only=True))

        self.store.set_participant_role("one", "Мария", "contractor")
        service.infer_participant_roles("one")
        maria = next(p for p in self.store.participants("one") if p["speaker"] == "Мария")
        self.assertEqual(maria["side"], "contractor")
        self.assertEqual(maria["source"], "manager")
        self.assertTrue(maria["confirmed"])

    def test_post_meeting_rejects_direction_reversal_even_if_verifier_supports_it(self):
        rows = [
            {**row("m1", "PM Xpage", "Дата запуска остаётся 18 сентября при условии, что материалы и доступы получим в срок.", "call"),
             "source_type": "meeting_transcript", "date_text": "10.09.2026", "channel": "Созвон"},
            {**row("m2", "Клиент", "Подтверждаю.", "call"),
             "source_type": "meeting_transcript", "date_text": "10.09.2026", "channel": "Созвон"},
            {**row("m3", "Клиент", "API-документацию и тестовые доступы передадим до пятницы.", "call"),
             "source_type": "meeting_transcript", "date_text": "10.09.2026", "channel": "Созвон"},
        ]
        self.store.import_rows(rows, "one")
        result = Service(self.store, ROOT, FalseActionPostMeetingClient()).post_meeting("one", "call", "10.09.2026")
        texts = [x["text"] for x in result["sections"]["client_actions"]]
        self.assertEqual(texts, ["API-документацию и тестовые доступы передадим до пятницы."])
        self.assertEqual(result["rejected"], 1)

    def test_post_meeting_merges_same_action_with_inflected_words(self):
        merged = merge_post_meeting([
            {"section": "our_actions", "text": "Проверим интеграцию в течение двух рабочих дней после получения доступов.",
             "evidence": [{"id": "M1", "quote": "После получения доступов мы проверим интеграцию в течение двух рабочих дней."}]},
            {"section": "our_actions", "text": "Возьмём на себя проверку интеграции.",
             "evidence": [{"id": "M2", "quote": "Проверку интеграции возьму на себя."}]},
        ])
        self.assertEqual(len(merged["our_actions"]), 1)
        self.assertEqual({e["id"] for e in merged["our_actions"][0]["evidence"]}, {"M1", "M2"})

    def test_post_meeting_keeps_distinct_related_decisions(self):
        merged = merge_post_meeting([
            {"section": "fixed", "text": "Альтернативная авторизация исключена из первой версии.",
             "evidence": [{"id": "M1", "quote": "Альтернативный вариант рассматриваем после запуска."}]},
            {"section": "fixed", "text": "Сценарий авторизации для первой версии остаётся текущим.",
             "evidence": [{"id": "M2", "quote": "Для первой версии оставляем текущий сценарий авторизации."}]},
        ])
        self.assertEqual(len(merged["fixed"]), 2)

    def test_registry_prompt_calls_out_open_questions_and_dependencies(self):
        self.assertIn("решение не принято", SYSTEM_REGISTRY_EXTRACT)
        self.assertIn("DEPENDENCY", SYSTEM_REGISTRY_EXTRACT)
        self.assertIn("отдельно оценим", SYSTEM_REGISTRY_EXTRACT)
        self.assertIn("PARTICIPANTS", SYSTEM_REGISTRY_EXTRACT)


if __name__ == "__main__":
    unittest.main()
