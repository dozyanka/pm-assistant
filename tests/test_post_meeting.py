"""Task tracker/Kanban and meeting-specific post-meeting regressions."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pm_app import __version__
from pm_app.db import Store
from pm_app.ollama import CHAT_MODEL, EMBED_MODEL, OllamaError
from pm_app.service import Service

ROOT = Path(__file__).resolve().parents[1]


def meeting_row(mid: str, text: str, date: str, speaker: str = "PM-01") -> dict:
    return {
        "project_id": "one", "source_id": "one-call", "message_id": mid,
        "source_type": "meeting_transcript", "speaker": speaker, "text": text,
        "occurred_at": None, "timezone": None, "date_text": date,
        "clock_time": "12:00", "channel": "Созвон",
    }


class PostMeetingClient:
    def __init__(self):
        self.calls = []
        self.last_chat_stats = {}

    def models(self):
        return {CHAT_MODEL: "chat-v1", EMBED_MODEL: "embed-v1"}

    def chat(self, system, user, schema, max_tokens=2048):
        self.calls.append((system, user, schema, max_tokens))
        props = schema.get("properties", {})
        if set(props) == {"key_results", "client_actions", "our_actions", "fixed", "clarify"}:
            rows = {}
            for line in user.splitlines():
                if line.startswith('["M'):
                    data = json.loads(line)
                    rows[data[0]] = data[5]
            return {
                "key_results": [{"text": "Согласовали обновлённый сценарий авторизации.",
                                 "evidence": [{"id": "M1", "quote": rows["M1"]}]}],
                "client_actions": [{"text": "Передать API-документацию — до конца недели.",
                                    "evidence": [{"id": "M2", "quote": rows["M2"]}]}],
                "our_actions": [{"text": "Проверить интеграцию после получения доступов.",
                                 "evidence": [{"id": "M3", "quote": rows["M3"]}]}],
                "fixed": [{"text": "Дополнительный функционал не входит в текущий объём.",
                           "evidence": [{"id": "M4", "quote": rows["M4"]}]}],
                "clarify": [{"text": "Не определён срок предоставления контента для раздела «Новости».",
                             "evidence": [{"id": "M5", "quote": rows["M5"]}]}],
            }
        if "checks" in props:
            candidates = json.loads(user.split("\nCANDIDATES:\n", 1)[1])
            return {"checks": [{"claim_number": c["claim_number"], "verdict": "supported"}
                               for c in candidates]}
        raise AssertionError("Unexpected schema")


class PostMeetingAndKanbanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db.sqlite3")
        self.store.create_project("one", "One")

    def tearDown(self):
        self.tmp.cleanup()

    def test_version_and_ui_surfaces(self):
        self.assertEqual(__version__, "1.0.0")
        html = (ROOT / "pm_app" / "static" / "index.html").read_text(encoding="utf-8")
        js = "\n".join(path.read_text(encoding="utf-8") for path in sorted((ROOT / "pm_app" / "static" / "js").glob("*.js")))
        self.assertIn('id="tab-tasks"', html)
        self.assertIn('id="tab-postmeeting"', html)
        self.assertIn('id="kanban"', html)
        self.assertIn('result.diagnostics && isAdmin()', js)
        self.assertIn('action:"move"', js)

    def test_kanban_move_is_manager_action(self):
        self.store.import_rows([{
            "project_id": "one", "source_id": "chat", "message_id": "m1", "source_type": "client_chat",
            "speaker": "PM", "text": "Подготовить оценку.", "occurred_at": None, "timezone": None,
        }], "one")
        rev = self.store.current("one")[0]
        self.store.save_registry_snapshot("one", None, "fp", "model", [{
            "local_id": "R1", "stable_key": "task-k", "kind": "TASK", "subject": "Оценка",
            "statement": "Подготовить оценку.", "lifecycle": "open", "responsible": "",
            "deadline_text": "", "deadline_iso": "",
            "evidence": [{"revision_id": rev["id"], "quote": "Подготовить оценку."}], "supersedes": [],
        }])
        entry = self.store.registry("one")["entries"][0]
        updated = self.store.registry_action("one", entry["id"], "move", {"lifecycle": "in_progress"})
        self.assertEqual(updated["lifecycle"], "in_progress")
        self.assertEqual(updated["review_status"], "confirmed")
        self.assertEqual(updated["origin"], "manager")
        self.assertEqual(self.store.registry_history("one", entry["id"])[-1]["action"], "move")
        with self.assertRaisesRegex(ValueError, "Invalid Kanban"):
            self.store.registry_action("one", entry["id"], "move", {"lifecycle": "cancelled"})

    def test_meeting_sessions_are_split_by_date(self):
        rows = [meeting_row("a", "Первая встреча.", "12.08.2026"),
                meeting_row("b", "Вторая встреча.", "13.08.2026")]
        self.store.import_rows(rows, "one")
        sessions = Service(self.store, ROOT, PostMeetingClient()).meeting_sessions("one")
        self.assertEqual({s["date_key"] for s in sessions}, {"12.08.2026", "13.08.2026"})
        self.assertTrue(all(s["message_count"] == 1 for s in sessions))

    def test_post_meeting_uses_only_selected_meeting_and_template_sections(self):
        rows = [
            meeting_row("m1", "Согласовали обновлённый сценарий авторизации.", "12.08.2026", "Клиент"),
            meeting_row("m2", "API-документацию передадим до конца недели.", "12.08.2026", "Клиент"),
            meeting_row("m3", "После получения доступов проверим интеграцию.", "12.08.2026", "PM-01"),
            meeting_row("m4", "Дополнительный функционал не входит в текущий объём.", "12.08.2026", "PM-01"),
            meeting_row("m5", "Срок контента для раздела Новости пока не определён.", "12.08.2026", "Клиент"),
            meeting_row("other", "SECRET_OTHER_MEETING", "13.08.2026", "Клиент"),
        ]
        self.store.import_rows(rows, "one")
        client = PostMeetingClient()
        result = Service(self.store, ROOT, client).post_meeting("one", "one-call", "12.08.2026")
        self.assertEqual(result["meeting_date_label"], "12.08.2026")
        self.assertEqual(result["message_count"], 5)
        self.assertEqual(set(result["sections"]), {"key_results", "client_actions", "our_actions", "fixed", "clarify"})
        self.assertEqual(result["accepted"], 5)
        self.assertTrue(all("SECRET_OTHER_MEETING" not in user for _, user, _, _ in client.calls))

    def test_post_meeting_rejects_quote_outside_source(self):
        self.store.import_rows([meeting_row("m1", "Фактический текст.", "12.08.2026")], "one")

        class BadClient(PostMeetingClient):
            def chat(self, system, user, schema, max_tokens=2048):
                if "key_results" in schema.get("properties", {}):
                    return {"key_results": [{"text": "Выдумка", "evidence": [{"id": "M1", "quote": "Нет такой цитаты"}]}],
                            "client_actions": [], "our_actions": [], "fixed": [], "clarify": []}
                return super().chat(system, user, schema, max_tokens)

        with self.assertRaises(OllamaError):
            Service(self.store, ROOT, BadClient()).post_meeting("one", "one-call", "12.08.2026")


if __name__ == "__main__":
    unittest.main()
