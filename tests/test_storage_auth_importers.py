"""Accounts, imports, project cleanup and message revision regressions."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

from pm_app import __version__
from pm_app.auth import AuthManager
from pm_app.db import Store
from pm_app.importers import parse_upload
from pm_app.service import Service

ROOT = Path(__file__).resolve().parents[1]
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def row(pid="one", mid="m1", text="Исходный текст"):
    return {"project_id": pid, "source_id": "chat", "source_type": "client_chat", "message_id": mid,
            "speaker": "PM", "text": text, "occurred_at": None, "timezone": None}


def _docx(paragraphs: list[tuple[str, str | None]]) -> bytes:
    def esc(s):
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    body=[]
    for first, second in paragraphs:
        if second is None:
            body.append(f'<w:p><w:r><w:t>{esc(first)}</w:t></w:r></w:p>')
        else:
            body.append(f'<w:p><w:r><w:t>{esc(first)}</w:t><w:br/><w:t>{esc(second)}</w:t></w:r></w:p>')
    xml=(f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
         f'<w:document xmlns:w="{W}"><w:body>{"".join(body)}<w:sectPr/></w:body></w:document>').encode()
    buf=io.BytesIO()
    with ZipFile(buf,"w",ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\"></Types>")
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


class StorageTests(unittest.TestCase):
    def test_version(self):
        self.assertEqual(__version__, "1.0.0")

    def test_message_edit_creates_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/"db.sqlite3");store.create_project("one","One");store.import_rows([row()],"one")
            message=store.current("one")[0]
            store.revise_message_text("one",message["message_fk"],"Исправленный текст")
            current=store.current("one")[0]
            self.assertEqual(current["raw"]["text"],"Исправленный текст")
            self.assertEqual(current["revision"],2)
            history=store.revision_history("one",message["message_fk"])
            self.assertEqual([x["raw"]["text"] for x in history],["Исходный текст","Исправленный текст"])

    def test_project_delete_removes_children(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/"db.sqlite3");store.create_project("one","One");store.import_rows([row()],"one")
            store.delete_project("one")
            self.assertEqual(store.projects(),[])
            with self.assertRaises(ValueError): store.require_project("one")

    @unittest.skipUnless((ROOT / "samples" / "messages.jsonl").exists() and (ROOT / "samples" / "source_project_1" / "messages.jsonl").exists(), "case-provided legacy fixtures are not included in the public repository")
    def test_legacy_duplicate_is_cleaned_on_restart_only_when_exact(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"db.sqlite3";store=Store(path);service=Service(store,ROOT)
            service.demo_import();service.source_project_1_import()
            self.assertIn("urbankey-source-example-1",{p["id"] for p in store.projects()})
            reopened=Store(path)
            ids={p["id"] for p in reopened.projects()}
            self.assertIn("urbankey",ids);self.assertNotIn("urbankey-source-example-1",ids)


class AuthTests(unittest.TestCase):
    def test_password_is_hashed_and_session_can_be_revoked(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/"db.sqlite3");auth=AuthManager(store)
            admin=auth.setup_first_admin("admin","Администратор","very-secret")
            self.assertEqual(admin["role"],"admin")
            user,token=auth.login("admin","very-secret")
            self.assertEqual(user["username"],"admin");self.assertIsNotNone(auth.user_for_token(token))
            with closing(store.connect()) as con:
                stored=con.execute("SELECT password_hash,password_salt FROM users WHERE username='admin'").fetchone()
                self.assertNotIn("very-secret",stored["password_hash"]);self.assertTrue(stored["password_salt"])
            auth.logout(token);self.assertIsNone(auth.user_for_token(token))


class ImporterTests(unittest.TestCase):
    def test_generic_docx_can_be_imported_into_selected_project(self):
        data=_docx([("Первая заметка",None),("Вторая заметка",None)])
        bundle=parse_upload("notes.docx",data,"one")
        self.assertEqual(len(bundle.rows),2);self.assertEqual({r["project_id"] for r in bundle.rows},{"one"})

    def test_large_company_shape_is_split_into_15_independent_projects(self):
        paragraphs=[("Большая синтетическая база данных",None),("Кейс GigaSchool: AI-ассистент проектного менеджера",None)]
        for index in range(1,16):
            paragraphs += [(f"{index:02d}. Project {index}",None),
                (f"Проект: PRJ-{index:02d} · Тип: test · Этап: Разработка",None),
                ("Каналы: MAX",None),("Объём коммуникации: 114 событий.",None),("Сырая коммуникация по проекту",None),("01.09.2026",None)]
            for n in range(114):
                hh=(9+(n//60))%24;mm=n%60
                paragraphs.append((f"{hh:02d}:{mm:02d} · MAX · клиент · PM-{index:02d}",f"Сообщение {n+1} проекта {index}."))
        bundle=parse_upload("large.docx",_docx(paragraphs))
        self.assertTrue(bundle.is_dataset);self.assertEqual(bundle.batch_name,"Большая синтетическая база")
        self.assertEqual(len(bundle.names),15);self.assertEqual(len(bundle.rows),1710)
        self.assertEqual(len({r["project_id"] for r in bundle.rows}),15)

    def test_preview_detects_changed_message_before_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/"db.sqlite3");store.create_project("one","One");store.import_rows([row()],"one")
            changed=row(text="Новый текст")
            preview=store.preview_import([changed],selected_project="one")
            self.assertEqual(preview["revised"],1);self.assertEqual(preview["added"],0)


if __name__ == "__main__":
    unittest.main()
