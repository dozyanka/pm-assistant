"""Human-friendly participant identities and contextual role review."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pm_app.db import Store, participant_identity_kind
from pm_app.service import Service

ROOT = Path(__file__).resolve().parents[1]


def row(mid: str, speaker: str, text: str, source_type: str = "internal_chat", source: str = "chat") -> dict:
    return {
        "project_id": "one", "source_id": source, "message_id": mid,
        "source_type": source_type, "speaker": speaker, "text": text,
        "occurred_at": None, "timezone": None,
    }


class ParticipantIdentityUITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db.sqlite3")
        self.store.create_project("one", "One")

    def tearDown(self):
        self.tmp.cleanup()

    def test_identity_kind_distinguishes_people_roles_and_service_accounts(self):
        self.assertEqual(participant_identity_kind("Анна К."), "person")
        self.assertEqual(participant_identity_kind("Разработчик"), "role_alias")
        self.assertEqual(participant_identity_kind("Аналитик"), "role_alias")
        self.assertEqual(participant_identity_kind("PM Xpage"), "role_alias")
        self.assertEqual(participant_identity_kind("product@industrypulse.test"), "service_account")

    def test_internal_role_alias_is_trusted_our_team_but_json_is_unchanged(self):
        original = row("m1", "Разработчик", "Проверю конфиг регионов.")
        self.store.import_rows([original], "one")
        person = self.store.participants("one")[0]
        self.assertEqual(person["identity_kind"], "role_alias")
        self.assertEqual(person["side"], "our_team")
        self.assertEqual(person["source"], "structural")
        self.assertTrue(person["confirmed"])
        self.assertEqual(self.store.current("one")[0]["raw"], original)

    def test_structural_role_overrides_old_unconfirmed_ai_suggestion(self):
        self.store.import_rows([row("m1", "Аналитик", "Проверю требования.")], "one")
        # Simulate a stale unconfirmed AI suggestion written before the new structural rule.
        self.store.save_participant_suggestions("one", [{"speaker": "Аналитик", "side": "client", "confidence": 62}])
        person = self.store.participants("one")[0]
        self.assertEqual(person["side"], "our_team")
        self.assertEqual(person["source"], "structural")
        self.assertTrue(person["confirmed"])

    def test_role_alias_in_meeting_is_not_guessed_from_job_title(self):
        self.store.import_rows([row("m1", "Разработчик", "Сделаем к пятнице.", "meeting_transcript", "call")], "one")
        person = self.store.participants("one")[0]
        self.assertEqual(person["identity_kind"], "role_alias")
        self.assertEqual(person["side"], "unknown")
        self.assertFalse(person["confirmed"])

    def test_participant_rows_include_context_examples_without_internal_ids(self):
        self.store.import_rows([
            row("m1", "Мария", "Первое характерное сообщение.", "client_chat"),
            row("m2", "Мария", "Второе характерное сообщение.", "client_chat"),
            row("m3", "Мария", "Третье характерное сообщение.", "client_chat"),
            row("m4", "Мария", "Последнее характерное сообщение.", "client_chat"),
        ], "one")
        person = self.store.participants("one")[0]
        self.assertEqual(person["message_count"], 4)
        self.assertEqual(len(person["examples"]), 3)
        self.assertEqual(person["examples"][0]["text"], "Первое характерное сообщение.")
        self.assertEqual(person["examples"][-1]["text"], "Последнее характерное сообщение.")
        self.assertNotIn("source_id", person["examples"][0])
        self.assertNotIn("message_id", person["examples"][0])

    @unittest.skipUnless((ROOT / "samples" / "messages.jsonl").exists(), "case-provided starter fixture is not included in the public repository")
    def test_industrypulse_email_and_role_labels_are_presented_correctly(self):
        # This reproduces the exact confusing identities from the shipped synthetic sample.
        class NoModel:
            pass
        service = Service(self.store, ROOT, NoModel())
        service.demo_import()
        people = {p["speaker"]: p for p in self.store.participants("industrypulse")}
        email = people["product@industrypulse.test"]
        self.assertEqual(email["identity_kind"], "service_account")
        self.assertEqual(email["side"], "unknown")
        self.assertTrue(email["examples"])
        developer = people["Разработчик"]
        analyst = people["Аналитик"]
        self.assertEqual((developer["identity_kind"], developer["side"], developer["source"]),
                         ("role_alias", "our_team", "structural"))
        self.assertEqual((analyst["identity_kind"], analyst["side"], analyst["source"]),
                         ("role_alias", "our_team", "structural"))

    def test_frontend_groups_identities_and_shows_example_messages(self):
        js = "\n".join(path.read_text(encoding="utf-8") for path in sorted((ROOT / "pm_app" / "static" / "js").glob("*.js")))
        html = (ROOT / "pm_app" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('participantGroup("Люди"', js)
        self.assertIn('participantGroup("Роли без имени"', js)
        self.assertIn('participantGroup("Служебные адреса"', js)
        self.assertIn('Показать сообщения', js)
        self.assertIn('Служебный адрес', js)
        self.assertNotIn('импортированный JSON/DOCX не изменяется', html)


if __name__ == "__main__":
    unittest.main()
