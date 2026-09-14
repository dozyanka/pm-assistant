"""Persistent originals, explicit revisions, and a rebuildable search index."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_ROWS = 5000
MAX_TEXT = 20000
PROJECT_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}\Z")
REGISTRY_KINDS = {"TASK", "DECISION", "PROMISE", "REQUIREMENT", "OPEN_QUESTION", "DEPENDENCY"}
REGISTRY_LIFECYCLES = {"proposed", "agreed", "open", "in_progress", "completed", "cancelled", "superseded", "blocked", "unknown"}
REGISTRY_REVIEWS = {"candidate", "confirmed", "edited", "excluded"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def required_string(value: Any, field: str, limit: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise ValueError(f"Invalid {field}: expected nonempty text, at most {limit} characters.")
    return value


def parse_jsonl(text: str) -> list[dict]:
    if not isinstance(text, str):
        raise ValueError("JSONL input must be text.")
    rows = []
    for line_no, line in enumerate(text.lstrip("\ufeff").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
            validate_row(row)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"JSONL line {line_no}: {exc}") from exc
        rows.append(row)
        if len(rows) > MAX_ROWS:
            raise ValueError(f"At most {MAX_ROWS} messages per import.")
    if not rows:
        raise ValueError("The JSONL file is empty.")
    return rows


def validate_row(row: Any) -> None:
    if not isinstance(row, dict):
        raise ValueError("Each JSONL line must be an object.")
    pid = required_string(row.get("project_id"), "project_id", 80)
    if not PROJECT_ID.fullmatch(pid):
        raise ValueError("project_id: use ASCII letters, digits, hyphen, dot or underscore.")
    for field in ("message_id", "source_id"):
        required_string(row.get(field), field, 250)
    required_string(row.get("text"), "text", MAX_TEXT)
    for field in ("speaker", "source_type", "timezone", "clock_time", "reply_to_message_id"):
        if row.get(field) is not None:
            required_string(row[field], field, 250)
    if row.get("topic_id") is not None:
        topic = required_string(row["topic_id"], "topic_id", 80)
        if not PROJECT_ID.fullmatch(topic):
            raise ValueError("Invalid topic_id.")
        required_string(row.get("topic_name"), "topic_name", 120)
    elif row.get("topic_name") is not None:
        raise ValueError("topic_name requires a topic_id.")
    occurred = row.get("occurred_at")
    if occurred is not None:
        required_string(occurred, "occurred_at", 80)
        try:
            dt = datetime.fromisoformat(occurred.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("occurred_at must be ISO 8601 with an explicit UTC offset, or null.") from exc
        if dt.tzinfo is None:
            raise ValueError("occurred_at must include a UTC offset (for example +05:00), or be null.")
    if len(canonical(row).encode("utf-8")) > 100000:
        raise ValueError("One message has too much metadata.")


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT OR IGNORE INTO meta(key,value) VALUES ('schema_version','1');
CREATE TABLE IF NOT EXISTS projects(
    id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources(
    id INTEGER PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    source_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    UNIQUE(project_id, source_id)
);
CREATE TABLE IF NOT EXISTS messages(
    id INTEGER PRIMARY KEY,
    source_fk INTEGER NOT NULL REFERENCES sources(id),
    message_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    UNIQUE(source_fk, message_id)
);
CREATE TABLE IF NOT EXISTS revisions(
    id INTEGER PRIMARY KEY,
    message_fk INTEGER NOT NULL REFERENCES messages(id),
    revision INTEGER NOT NULL,
    raw_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    UNIQUE(message_fk, revision)
);
CREATE INDEX IF NOT EXISTS idx_revisions_message ON revisions(message_fk, revision DESC);
CREATE TABLE IF NOT EXISTS search_indexes(
    project_id TEXT PRIMARY KEY REFERENCES projects(id),
    fingerprint TEXT NOT NULL, model_digest TEXT NOT NULL, built_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS search_chunks(
    id INTEGER PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    revision_ids TEXT NOT NULL,
    text TEXT NOT NULL,
    vector TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_project ON search_chunks(project_id);
CREATE TABLE IF NOT EXISTS answers(
    id INTEGER PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    created_at TEXT NOT NULL,
    question TEXT NOT NULL,
    response_json TEXT NOT NULL
);
"""

REGISTRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS registry_entries(
    id INTEGER PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    topic_id TEXT NOT NULL DEFAULT '',
    stable_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    subject TEXT NOT NULL,
    statement TEXT NOT NULL,
    lifecycle TEXT NOT NULL,
    responsible TEXT,
    deadline_text TEXT,
    deadline_iso TEXT,
    review_status TEXT NOT NULL,
    origin TEXT NOT NULL,
    stale INTEGER NOT NULL DEFAULT 0,
    source_fingerprint TEXT NOT NULL,
    model_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id, topic_id, stable_key)
);
CREATE INDEX IF NOT EXISTS idx_registry_project_topic ON registry_entries(project_id, topic_id, id);
CREATE TABLE IF NOT EXISTS registry_evidence(
    entry_id INTEGER NOT NULL REFERENCES registry_entries(id) ON DELETE CASCADE,
    revision_id INTEGER NOT NULL REFERENCES revisions(id),
    quote TEXT NOT NULL,
    PRIMARY KEY(entry_id, revision_id)
);
CREATE INDEX IF NOT EXISTS idx_registry_evidence_revision ON registry_evidence(revision_id);
CREATE TABLE IF NOT EXISTS registry_relations(
    entry_id INTEGER NOT NULL REFERENCES registry_entries(id) ON DELETE CASCADE,
    supersedes_entry_id INTEGER NOT NULL REFERENCES registry_entries(id) ON DELETE CASCADE,
    PRIMARY KEY(entry_id, supersedes_entry_id),
    CHECK(entry_id <> supersedes_entry_id)
);
CREATE TABLE IF NOT EXISTS registry_history(
    id INTEGER PRIMARY KEY,
    entry_id INTEGER NOT NULL REFERENCES registry_entries(id) ON DELETE CASCADE,
    changed_at TEXT NOT NULL,
    action TEXT NOT NULL,
    snapshot_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_registry_history_entry ON registry_history(entry_id, id);
CREATE TABLE IF NOT EXISTS registry_runs(
    id INTEGER PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    topic_id TEXT NOT NULL DEFAULT '',
    source_fingerprint TEXT NOT NULL,
    model_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    extracted_count INTEGER NOT NULL,
    accepted_count INTEGER NOT NULL,
    rejected_count INTEGER NOT NULL
);
"""

ACCOUNT_IMPORT_SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    password_salt TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin','pm')),
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions(
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS import_batches(
    id INTEGER PRIMARY KEY,
    batch_key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    file_name TEXT NOT NULL,
    file_format TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    project_count INTEGER NOT NULL,
    message_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS import_batch_projects(
    batch_id INTEGER NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    PRIMARY KEY(batch_id, project_id)
);
CREATE INDEX IF NOT EXISTS idx_import_batch_projects_project ON import_batch_projects(project_id);
"""

PARTICIPANT_SCHEMA = """
CREATE TABLE IF NOT EXISTS participant_roles(
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    speaker TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('our_team','client','contractor','other','unknown')),
    source TEXT NOT NULL CHECK(source IN ('ai','manager')),
    confidence INTEGER NOT NULL DEFAULT 0 CHECK(confidence BETWEEN 0 AND 100),
    confirmed INTEGER NOT NULL DEFAULT 0 CHECK(confirmed IN (0,1)),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(project_id, speaker)
);
CREATE INDEX IF NOT EXISTS idx_participant_roles_project ON participant_roles(project_id, speaker);
"""

PARTICIPANT_SIDES = {'our_team','client','contractor','other','unknown'}

# Speaker labels in imported archives are not always real people. Keep this
# classification outside the imported JSON so old JSONL/DOCX remains compatible.
_PARTICIPANT_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+\Z", re.IGNORECASE)
_ROLE_ALIAS_PATTERNS = (
    r"разработчик(?:\s+\d+)?", r"аналитик", r"qa(?:\s+поддержки)?", r"тестировщик",
    r"дизайнер", r"техлид", r"технический\s+лидер", r"специалист\s+техподдержки",
    r"техподдержка", r"руководитель\s+проекта", r"руководитель\s+разработки",
    r"коммерческий\s+директор", r"продакт", r"product\s+owner", r"project\s+manager",
    r"менеджер\s+проекта", r"клиент", r"заказчик", r"customer", r"client",
    r"подрядчик", r"contractor", r"pm(?:[-_ ]?\d+)?", r"pm\s+xpage",
)
_ROLE_ALIAS_RE = re.compile(r"(?:" + "|".join(_ROLE_ALIAS_PATTERNS) + r")\Z", re.IGNORECASE)


def participant_identity_kind(speaker: str | None) -> str:
    """Classify a speaker label for UI only; never rewrite the source message."""
    if not isinstance(speaker, str) or not speaker.strip():
        return "unknown"
    value = ' '.join(speaker.strip().split())
    if _PARTICIPANT_EMAIL.fullmatch(value):
        return "service_account"
    normalized = value.casefold().replace('ё', 'е')
    if _ROLE_ALIAS_RE.fullmatch(normalized):
        return "role_alias"
    return "person"


def structural_participant_side(speaker: str | None, source_types: set[str]) -> tuple[str | None, str | None]:
    """Resolve only identities whose side follows from explicit import structure."""
    explicit = explicit_participant_side(speaker)
    if explicit is not None:
        return explicit, "explicit"
    kind = participant_identity_kind(speaker)
    if kind == "role_alias" and source_types and source_types <= {"internal_chat"}:
        return "our_team", "structural"
    if kind == "service_account" and isinstance(speaker, str):
        domain = speaker.rsplit("@", 1)[-1].casefold()
        if domain == "xpage.test" or domain.startswith("xpage.") or ".xpage." in domain:
            return "our_team", "structural"
    return None, None


def explicit_participant_side(speaker: str | None) -> str | None:
    """Return a side only when the speaker label itself makes it explicit.

    This is deliberately conservative. It is not LLM inference and it does not alter
    imported message JSON.
    """
    if not isinstance(speaker, str) or not speaker.strip():
        return None
    value = ' '.join(speaker.casefold().replace('ё','е').split())
    if 'xpage' in value:
        return 'our_team'
    if value in {'клиент','заказчик','customer','client'} or value.startswith('клиент '):
        return 'client'
    if 'подрядчик' in value or 'contractor' in value:
        return 'contractor'
    if re.fullmatch(r'pm(?:[-_ ]?\d+)?', value, flags=re.IGNORECASE):
        return 'our_team'
    return None


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as con:
            con.executescript(SCHEMA)
            row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            version = row[0] if row else "1"
            if version == "1":
                con.executescript(REGISTRY_SCHEMA)
                con.executescript(ACCOUNT_IMPORT_SCHEMA)
                con.executescript(PARTICIPANT_SCHEMA)
                con.execute("UPDATE meta SET value='4' WHERE key='schema_version'")
            elif version == "2":
                con.executescript(REGISTRY_SCHEMA)
                con.executescript(ACCOUNT_IMPORT_SCHEMA)
                con.executescript(PARTICIPANT_SCHEMA)
                con.execute("UPDATE meta SET value='4' WHERE key='schema_version'")
            elif version == "3":
                con.executescript(REGISTRY_SCHEMA)
                con.executescript(ACCOUNT_IMPORT_SCHEMA)
                con.executescript(PARTICIPANT_SCHEMA)
                con.execute("UPDATE meta SET value='4' WHERE key='schema_version'")
            elif version == "4":
                con.executescript(REGISTRY_SCHEMA)
                con.executescript(ACCOUNT_IMPORT_SCHEMA)
                con.executescript(PARTICIPANT_SCHEMA)
            else:
                raise ValueError("Unsupported database version. Do not overwrite an existing database.")
            con.commit()
        self.cleanup_legacy_demo_duplicates()

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=20)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        return con

    def _delete_project_in_connection(self, con: sqlite3.Connection, pid: str) -> None:
        # Explicit deletion order keeps compatibility with databases created before cascades were added.
        con.execute("DELETE FROM import_batch_projects WHERE project_id=?", (pid,))
        entry_ids = [r[0] for r in con.execute("SELECT id FROM registry_entries WHERE project_id=?", (pid,))]
        for eid in entry_ids:
            con.execute("DELETE FROM registry_evidence WHERE entry_id=?", (eid,))
            con.execute("DELETE FROM registry_relations WHERE entry_id=? OR supersedes_entry_id=?", (eid, eid))
            con.execute("DELETE FROM registry_history WHERE entry_id=?", (eid,))
        con.execute("DELETE FROM registry_entries WHERE project_id=?", (pid,))
        con.execute("DELETE FROM registry_runs WHERE project_id=?", (pid,))
        con.execute("DELETE FROM participant_roles WHERE project_id=?", (pid,))
        con.execute("DELETE FROM answers WHERE project_id=?", (pid,))
        con.execute("DELETE FROM search_chunks WHERE project_id=?", (pid,))
        con.execute("DELETE FROM search_indexes WHERE project_id=?", (pid,))
        source_ids = [r[0] for r in con.execute("SELECT id FROM sources WHERE project_id=?", (pid,))]
        for sid in source_ids:
            message_ids = [r[0] for r in con.execute("SELECT id FROM messages WHERE source_fk=?", (sid,))]
            for mid in message_ids:
                con.execute("DELETE FROM revisions WHERE message_fk=?", (mid,))
            con.execute("DELETE FROM messages WHERE source_fk=?", (sid,))
        con.execute("DELETE FROM sources WHERE project_id=?", (pid,))
        con.execute("DELETE FROM projects WHERE id=?", (pid,))
        # Drop empty dataset cards after their last project is removed.
        con.execute("DELETE FROM import_batches WHERE id NOT IN (SELECT DISTINCT batch_id FROM import_batch_projects)")

    def delete_project(self, pid: str) -> None:
        self.require_project(pid)
        with closing(self.connect()) as con, con:
            self._delete_project_in_connection(con, pid)

    def rename_project(self, pid: str, name: str) -> None:
        self.require_project(pid)
        required_string(name, "project name", 120)
        with closing(self.connect()) as con, con:
            con.execute("UPDATE projects SET name=? WHERE id=?", (name.strip(), pid))

    def cleanup_legacy_demo_duplicates(self) -> None:
        """Remove only the known patch-07 UrbanKey duplicate when it is byte-for-byte equivalent.

        This is deliberately conservative: arbitrary projects are never removed automatically.
        """
        legacy = "urbankey-source-example-1"
        try:
            with closing(self.connect()) as con, con:
                if con.execute("SELECT 1 FROM projects WHERE id=?", (legacy,)).fetchone() is None:
                    return
                if con.execute("SELECT 1 FROM projects WHERE id='urbankey'").fetchone() is None:
                    return
                def rows(pid):
                    out=[]
                    sql="""SELECT r.raw_json FROM revisions r JOIN messages m ON m.id=r.message_fk
                           JOIN sources s ON s.id=m.source_fk WHERE s.project_id=? AND r.revision=(
                           SELECT MAX(r2.revision) FROM revisions r2 WHERE r2.message_fk=m.id)
                           ORDER BY s.id,m.position"""
                    for rr in con.execute(sql,(pid,)):
                        raw=json.loads(rr[0]); raw["project_id"]="urbankey"
                        raw.pop("original_project_id",None); raw.pop("original_project_name",None)
                        out.append(canonical(raw))
                    return out
                normal, duplicate = rows("urbankey"), rows(legacy)
                if len(normal)==34 and normal==duplicate:
                    self._delete_project_in_connection(con, legacy)
        except sqlite3.Error:
            # Startup must not be blocked by optional cleanup.
            return

    def preview_import(self, rows: list[dict], selected_project: str | None = None,
                       names: dict[str, str] | None = None) -> dict:
        if not isinstance(rows, list) or not 0 < len(rows) <= MAX_ROWS:
            raise ValueError("Invalid number of import rows.")
        added = unchanged = revised = 0
        affected: set[str] = set()
        new_projects: set[str] = set()
        seen: dict[tuple[str,str,str], str] = {}
        with closing(self.connect()) as con:
            for row in rows:
                validate_row(row)
                pid=row["project_id"]
                if selected_project is not None and pid != selected_project:
                    raise ValueError("Import rejected: a row belongs to a different project.")
                affected.add(pid)
                if con.execute("SELECT 1 FROM projects WHERE id=?",(pid,)).fetchone() is None:
                    if names is None or pid not in names:
                        raise ValueError("Create/select the project before importing its messages.")
                    new_projects.add(pid)
                key=(pid,row["source_id"],row["message_id"]); h=digest(row)
                if key in seen and seen[key] != h:
                    raise ValueError("Import contains two different versions of the same message ID.")
                seen[key]=h
                current=con.execute("""SELECT r.content_hash FROM revisions r JOIN messages m ON m.id=r.message_fk
                    JOIN sources s ON s.id=m.source_fk WHERE s.project_id=? AND s.source_id=? AND m.message_id=?
                    ORDER BY r.revision DESC LIMIT 1""", key).fetchone()
                if current is None: added += 1
                elif current[0] == h: unchanged += 1
                else: revised += 1
        return {"added": added, "unchanged": unchanged, "revised": revised,
                "new_projects": sorted(new_projects), "affected_projects": sorted(affected),
                "message_count": len(rows)}

    def record_import_batch(self, batch_key: str, name: str, file_name: str, file_format: str,
                            content_hash: str, project_ids: list[str], message_count: int) -> dict:
        for value, field, limit in ((batch_key,"batch key",200),(name,"batch name",160),
                                    (file_name,"file name",260),(file_format,"file format",30),
                                    (content_hash,"content hash",128)):
            required_string(value, field, limit)
        project_ids=sorted(set(project_ids))
        if not project_ids:
            raise ValueError("Import batch has no projects.")
        now=utc_now()
        with closing(self.connect()) as con, con:
            for pid in project_ids:
                if con.execute("SELECT 1 FROM projects WHERE id=?",(pid,)).fetchone() is None:
                    raise ValueError("Cannot attach an import batch to an unknown project.")
            row=con.execute("SELECT id,created_at FROM import_batches WHERE batch_key=?",(batch_key,)).fetchone()
            if row is None:
                cur=con.execute("""INSERT INTO import_batches(batch_key,name,file_name,file_format,content_hash,created_at,updated_at,project_count,message_count)
                    VALUES (?,?,?,?,?,?,?,?,?)""",(batch_key,name,file_name,file_format,content_hash,now,now,len(project_ids),message_count))
                bid=cur.lastrowid
            else:
                bid=row["id"]
                con.execute("""UPDATE import_batches SET name=?,file_name=?,file_format=?,content_hash=?,updated_at=?,project_count=?,message_count=? WHERE id=?""",
                            (name,file_name,file_format,content_hash,now,len(project_ids),message_count,bid))
                con.execute("DELETE FROM import_batch_projects WHERE batch_id=?",(bid,))
            con.executemany("INSERT OR IGNORE INTO import_batch_projects(batch_id,project_id) VALUES (?,?)",
                            [(bid,pid) for pid in project_ids])
        return {"id": bid, "name": name, "project_count": len(project_ids), "message_count": message_count}

    def import_batches(self) -> list[dict]:
        with closing(self.connect()) as con:
            result=[]
            for row in con.execute("SELECT * FROM import_batches ORDER BY updated_at DESC,id DESC"):
                item=dict(row)
                item["projects"]=[dict(r) for r in con.execute("""SELECT p.id,p.name,
                    (SELECT COUNT(*) FROM messages m JOIN sources s ON s.id=m.source_fk WHERE s.project_id=p.id) message_count
                    FROM import_batch_projects bp JOIN projects p ON p.id=bp.project_id WHERE bp.batch_id=? ORDER BY p.name""",(row["id"],))]
                result.append(item)
            return result

    def revise_message_text(self, pid: str, message_fk: int, text: str) -> dict:
        self.require_project(pid)
        if type(message_fk) is not int or message_fk <= 0:
            raise ValueError("Invalid message.")
        required_string(text, "message text", MAX_TEXT)
        with closing(self.connect()) as con:
            row=con.execute("""SELECT r.raw_json FROM revisions r JOIN messages m ON m.id=r.message_fk
                JOIN sources s ON s.id=m.source_fk WHERE s.project_id=? AND m.id=? ORDER BY r.revision DESC LIMIT 1""",
                (pid,message_fk)).fetchone()
        if row is None:
            raise ValueError("Unknown message.")
        raw=json.loads(row[0]); raw["text"]=text.strip()
        result=self.import_rows([raw],selected_project=pid,allow_revisions=True)
        current=next((m for m in self.current(pid) if m["message_fk"]==message_fk),None)
        return {"result":result,"message":current}

    def create_project(self, pid: str, name: str) -> None:
        if not isinstance(pid, str) or not PROJECT_ID.fullmatch(pid):
            raise ValueError("Invalid project_id: use ASCII letters, digits, dot, hyphen or underscore.")
        required_string(name, "project name", 120)
        with closing(self.connect()) as con, con:
            con.execute("INSERT INTO projects VALUES (?,?,?)", (pid, name.strip(), utc_now()))

    def projects(self) -> list[dict]:
        with closing(self.connect()) as con:
            return [dict(r) for r in con.execute("""
              SELECT p.id, p.name,
                (SELECT COUNT(*) FROM sources s WHERE s.project_id=p.id) source_count,
                (SELECT COUNT(*) FROM messages m JOIN sources s ON s.id=m.source_fk
                 WHERE s.project_id=p.id) message_count,
                (SELECT MAX(r.imported_at) FROM revisions r JOIN messages m ON m.id=r.message_fk
                 JOIN sources s ON s.id=m.source_fk WHERE s.project_id=p.id) last_import,
                (SELECT built_at FROM search_indexes i WHERE i.project_id=p.id) indexed_at,
                (SELECT COUNT(*) FROM registry_entries e WHERE e.project_id=p.id AND e.review_status<>'excluded') registry_count
              FROM projects p ORDER BY p.created_at, p.id
            """)]

    def require_project(self, pid: str) -> dict:
        with closing(self.connect()) as con:
            row = con.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
            if row is None:
                raise ValueError("Unknown project.")
            return dict(row)

    def import_rows(self, rows: list[dict], selected_project: str | None = None,
                    names: dict[str, str] | None = None, allow_revisions: bool = False) -> dict:
        if not isinstance(rows, list) or not 0 < len(rows) <= MAX_ROWS:
            raise ValueError("Invalid number of import rows.")
        seen = {}
        for row in rows:
            validate_row(row)
            if selected_project is not None and row["project_id"] != selected_project:
                raise ValueError("Import rejected: a row belongs to a different project.")
            key = (row["project_id"], row["source_id"], row["message_id"])
            h = digest(row)
            if key in seen and seen[key] != h:
                raise ValueError("Import contains two different versions of the same message ID.")
            seen[key] = h
        counts = {"added": 0, "unchanged": 0, "revised": 0}
        changed_projects = set()
        now = utc_now()
        with closing(self.connect()) as con, con:
            for row in rows:
                pid = row["project_id"]
                project = con.execute("SELECT id FROM projects WHERE id=?", (pid,)).fetchone()
                if project is None:
                    if names is None or pid not in names:
                        raise ValueError("Create/select the project before importing its messages.")
                    required_string(names[pid], "project name", 120)
                    con.execute("INSERT INTO projects VALUES (?,?,?)", (pid, names[pid], now))
                src_type = row.get("source_type") or "unspecified"
                con.execute("INSERT OR IGNORE INTO sources(project_id,source_id,source_type) VALUES (?,?,?)",
                            (pid, row["source_id"], src_type))
                source = con.execute("SELECT * FROM sources WHERE project_id=? AND source_id=?",
                                     (pid, row["source_id"])).fetchone()
                if source["source_type"] != src_type:
                    raise ValueError("A source_id cannot change its source_type in an import.")
                msg = con.execute("SELECT * FROM messages WHERE source_fk=? AND message_id=?",
                                  (source["id"], row["message_id"])).fetchone()
                if msg is None:
                    pos = con.execute("SELECT COALESCE(MAX(position),0)+1 FROM messages WHERE source_fk=?",
                                      (source["id"],)).fetchone()[0]
                    cur = con.execute("INSERT INTO messages(source_fk,message_id,position) VALUES (?,?,?)",
                                      (source["id"], row["message_id"], pos))
                    mid = cur.lastrowid
                    revision = 1
                    counts["added"] += 1
                else:
                    mid = msg["id"]
                    previous = con.execute("SELECT * FROM revisions WHERE message_fk=? ORDER BY revision DESC LIMIT 1",
                                           (mid,)).fetchone()
                    if previous["content_hash"] == digest(row):
                        counts["unchanged"] += 1
                        continue
                    if not allow_revisions:
                        raise ValueError("Existing message has changed. Import cancelled; explicitly enable revisions to keep both versions.")
                    revision = previous["revision"] + 1
                    counts["revised"] += 1
                con.execute("INSERT INTO revisions(message_fk,revision,raw_json,content_hash,imported_at) VALUES (?,?,?,?,?)",
                            (mid, revision, canonical(row), digest(row), now))
                changed_projects.add(pid)
            for pid in changed_projects:
                con.execute("DELETE FROM search_indexes WHERE project_id=?", (pid,))
                con.execute("DELETE FROM search_chunks WHERE project_id=?", (pid,))
        return counts

    def current(self, pid: str, source_id: str | None = None) -> list[dict]:
        self.require_project(pid)
        with closing(self.connect()) as con:
            sql = """SELECT r.*, m.message_id, m.position, s.source_id, s.source_type, s.id source_order
                     FROM revisions r JOIN messages m ON m.id=r.message_fk
                     JOIN sources s ON s.id=m.source_fk
                     WHERE s.project_id=? AND r.revision=(
                       SELECT MAX(r2.revision) FROM revisions r2 WHERE r2.message_fk=m.id)
                  """
            params: list[Any] = [pid]
            if source_id:
                sql += " AND s.source_id=?"
                params.append(source_id)
            sql += " ORDER BY s.id,m.position"
            result = []
            for row in con.execute(sql, params):
                data = dict(row)
                data["raw"] = json.loads(data.pop("raw_json"))
                data["label"] = "M" + str(data["id"])
                result.append(data)
            return result

    def revision_history(self, pid: str, mid: int) -> list[dict]:
        with closing(self.connect()) as con:
            result = con.execute("""SELECT r.* FROM revisions r JOIN messages m ON m.id=r.message_fk
                JOIN sources s ON s.id=m.source_fk WHERE s.project_id=? AND m.id=? ORDER BY r.revision""",
                (pid, mid))
            return [{"id": r["id"], "revision": r["revision"], "imported_at": r["imported_at"],
                     "raw": json.loads(r["raw_json"])} for r in result]

    def index(self, pid: str) -> tuple[dict | None, list[dict]]:
        with closing(self.connect()) as con:
            row = con.execute("SELECT * FROM search_indexes WHERE project_id=?", (pid,)).fetchone()
            chunks = []
            for chunk in con.execute("SELECT * FROM search_chunks WHERE project_id=? ORDER BY id", (pid,)):
                chunks.append({**dict(chunk), "revision_ids": json.loads(chunk["revision_ids"]),
                               "vector": json.loads(chunk["vector"])})
            return (dict(row) if row else None), chunks

    def save_index(self, pid: str, fingerprint: str, model_digest: str, chunks: list[dict]) -> None:
        with closing(self.connect()) as con, con:
            con.execute("DELETE FROM search_chunks WHERE project_id=?", (pid,))
            con.executemany("INSERT INTO search_chunks(project_id,revision_ids,text,vector) VALUES (?,?,?,?)",
                [(pid, canonical(c["revision_ids"]), c["text"], canonical(c["vector"])) for c in chunks])
            con.execute("INSERT OR REPLACE INTO search_indexes VALUES (?,?,?,?)", (pid, fingerprint, model_digest, utc_now()))

    def save_answer(self, pid: str, question: str, response: dict) -> int:
        with closing(self.connect()) as con, con:
            return con.execute("INSERT INTO answers(project_id,created_at,question,response_json) VALUES (?,?,?,?)",
                               (pid, utc_now(), question, canonical(response))).lastrowid


    def revision_cards(self, pid: str, revision_ids: list[int]) -> dict[int, dict]:
        """Return immutable source cards for exact revision IDs, scoped to one project.

        Large registries may reference more than SQLite's comfortable single-IN-clause size,
        so IDs are deduplicated and fetched in bounded batches.
        """
        self.require_project(pid)
        if not revision_ids:
            return {}
        if any(type(x) is not int or x <= 0 for x in revision_ids) or len(revision_ids) > 5000:
            raise ValueError("Invalid revision list.")
        unique_ids = list(dict.fromkeys(revision_ids))
        result: dict[int, dict] = {}
        with closing(self.connect()) as con:
            for start in range(0, len(unique_ids), 400):
                batch = unique_ids[start:start + 400]
                placeholders = ",".join("?" for _ in batch)
                rows = con.execute(f"""SELECT r.*, m.message_id, m.id message_fk, s.source_id, s.source_type
                    FROM revisions r JOIN messages m ON m.id=r.message_fk
                    JOIN sources s ON s.id=m.source_fk
                    WHERE s.project_id=? AND r.id IN ({placeholders})""", [pid, *batch])
                for r in rows:
                    raw = json.loads(r["raw_json"])
                    result[r["id"]] = {"id": "M" + str(r["id"]), "message_fk": r["message_fk"],
                        "revision": r["revision"], "message_id": r["message_id"],
                        "source_id": r["source_id"], "source_type": r["source_type"],
                        "raw": raw, "imported_at": r["imported_at"]}
        return result

    @staticmethod
    def _registry_snapshot(row: sqlite3.Row | dict) -> dict:
        d = dict(row)
        return {k: d.get(k) for k in ("id", "project_id", "topic_id", "stable_key", "kind",
            "subject", "statement", "lifecycle", "responsible", "deadline_text", "deadline_iso",
            "review_status", "origin", "stale", "source_fingerprint", "model_digest",
            "created_at", "updated_at")}

    def save_registry_snapshot(self, pid: str, topic_id: str | None, source_fingerprint: str,
                               model_digest: str, entries: list[dict], rejected_count: int = 0,
                               extracted_count: int | None = None) -> dict:
        """Upsert verified model candidates without overwriting manager-reviewed content."""
        self.require_project(pid)
        topic = topic_id or ""
        if topic and not PROJECT_ID.fullmatch(topic):
            raise ValueError("Invalid topic_id.")
        if not isinstance(source_fingerprint, str) or not source_fingerprint:
            raise ValueError("Missing registry source fingerprint.")
        if not isinstance(model_digest, str) or not model_digest:
            raise ValueError("Missing registry model digest.")
        if not isinstance(entries, list) or len(entries) > 300:
            raise ValueError("Invalid registry entry list.")
        if type(rejected_count) is not int or rejected_count < 0:
            raise ValueError("Invalid registry rejected count.")
        if extracted_count is None:
            extracted_count = len(entries) + rejected_count
        if type(extracted_count) is not int or extracted_count < len(entries) + rejected_count:
            raise ValueError("Invalid registry extracted count.")
        now = utc_now()
        stats = {"added": 0, "refreshed": 0, "preserved_reviewed": 0, "stale_candidates": 0}
        local_to_db: dict[str, int] = {}
        stable_to_db: dict[str, int] = {}
        with closing(self.connect()) as con, con:
            # A repeated refresh over the exact same source snapshot must not make valid
            # candidates flap between active/stale just because local model extraction is
            # nondeterministic. Repair stale flags left by older versions for candidates that
            # were already verified on this exact snapshot, then stale only candidates last
            # seen on an older source fingerprint.
            con.execute("UPDATE registry_entries SET stale=0,updated_at=? WHERE project_id=? AND topic_id=? "
                        "AND review_status='candidate' AND origin='model' AND source_fingerprint=?",
                        (now, pid, topic, source_fingerprint))
            con.execute("UPDATE registry_entries SET stale=1,updated_at=? WHERE project_id=? AND topic_id=? "
                        "AND review_status='candidate' AND origin='model' AND source_fingerprint<>?",
                        (now, pid, topic, source_fingerprint))
            for item in entries:
                stable = item["stable_key"]
                existing = con.execute("SELECT * FROM registry_entries WHERE project_id=? AND topic_id=? AND stable_key=?",
                                       (pid, topic, stable)).fetchone()
                values = (item["kind"], item["subject"], item["statement"], item["lifecycle"],
                          item.get("responsible") or None, item.get("deadline_text") or None,
                          item.get("deadline_iso") or None, source_fingerprint, model_digest, now)
                if existing is None:
                    cur = con.execute("""INSERT INTO registry_entries(project_id,topic_id,stable_key,kind,subject,statement,
                        lifecycle,responsible,deadline_text,deadline_iso,review_status,origin,stale,source_fingerprint,
                        model_digest,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,'candidate','model',0,?,?,?,?)""",
                        (pid, topic, stable, *values[:-1], now, now))
                    eid = cur.lastrowid
                    stats["added"] += 1
                    row = con.execute("SELECT * FROM registry_entries WHERE id=?", (eid,)).fetchone()
                    con.execute("INSERT INTO registry_history(entry_id,changed_at,action,snapshot_json) VALUES (?,?,?,?)",
                                (eid, now, "extracted", canonical(self._registry_snapshot(row))))
                else:
                    eid = existing["id"]
                    if existing["review_status"] in ("confirmed", "edited", "excluded"):
                        con.execute("UPDATE registry_entries SET stale=0,source_fingerprint=?,model_digest=?,updated_at=? WHERE id=?",
                                    (source_fingerprint, model_digest, now, eid))
                        stats["preserved_reviewed"] += 1
                    else:
                        before = self._registry_snapshot(existing)
                        con.execute("""UPDATE registry_entries SET kind=?,subject=?,statement=?,lifecycle=?,responsible=?,
                            deadline_text=?,deadline_iso=?,stale=0,source_fingerprint=?,model_digest=?,updated_at=? WHERE id=?""",
                            (*values, eid))
                        after_row = con.execute("SELECT * FROM registry_entries WHERE id=?", (eid,)).fetchone()
                        after = self._registry_snapshot(after_row)
                        if any(before.get(k) != after.get(k) for k in ("kind","subject","statement","lifecycle","responsible","deadline_text","deadline_iso")):
                            con.execute("INSERT INTO registry_history(entry_id,changed_at,action,snapshot_json) VALUES (?,?,?,?)",
                                        (eid, now, "model_refresh", canonical(after)))
                        stats["refreshed"] += 1
                local_to_db[item["local_id"]] = eid
                stable_to_db[stable] = eid
                # Evidence is immutable source material. Refresh it for the same semantic candidate,
                # even if the manager edited display fields.
                con.execute("DELETE FROM registry_evidence WHERE entry_id=?", (eid,))
                for ev in item["evidence"]:
                    con.execute("INSERT INTO registry_evidence(entry_id,revision_id,quote) VALUES (?,?,?)",
                                (eid, ev["revision_id"], ev["quote"]))
            touched = set(local_to_db.values())
            for eid in touched:
                con.execute("DELETE FROM registry_relations WHERE entry_id=?", (eid,))
            for item in entries:
                eid = local_to_db[item["local_id"]]
                for old_local in item.get("supersedes", []):
                    old_id = local_to_db.get(old_local)
                    if old_id and old_id != eid:
                        con.execute("INSERT OR IGNORE INTO registry_relations(entry_id,supersedes_entry_id) VALUES (?,?)",
                                    (eid, old_id))
            stats["stale_candidates"] = con.execute("SELECT COUNT(*) FROM registry_entries WHERE project_id=? AND topic_id=? "
                "AND review_status='candidate' AND stale=1", (pid, topic)).fetchone()[0]
            con.execute("INSERT INTO registry_runs(project_id,topic_id,source_fingerprint,model_digest,created_at,extracted_count,accepted_count,rejected_count) "
                        "VALUES (?,?,?,?,?,?,?,?)", (pid, topic, source_fingerprint, model_digest, now,
                        extracted_count, len(entries), rejected_count))
        return stats

    def registry(self, pid: str, topic_id: str | None = None) -> dict:
        self.require_project(pid)
        topic = topic_id
        with closing(self.connect()) as con:
            sql = "SELECT * FROM registry_entries WHERE project_id=?"
            params: list[Any] = [pid]
            if topic is not None:
                sql += " AND topic_id=?"
                params.append(topic or "")
            sql += " ORDER BY CASE review_status WHEN 'excluded' THEN 1 ELSE 0 END, stale, id DESC"
            rows = [dict(r) for r in con.execute(sql, params)]
            evidence_ids = [r["revision_id"] for r in con.execute("""SELECT ev.revision_id FROM registry_evidence ev
                JOIN registry_entries e ON e.id=ev.entry_id WHERE e.project_id=?""", (pid,))]
        cards = self.revision_cards(pid, evidence_ids) if evidence_ids else {}
        with closing(self.connect()) as con:
            for row in rows:
                evidence = []
                for ev in con.execute("SELECT revision_id,quote FROM registry_evidence WHERE entry_id=? ORDER BY revision_id", (row["id"],)):
                    card = cards.get(ev["revision_id"] )
                    if card:
                        evidence.append({"quote": ev["quote"], "source": card})
                row["evidence"] = evidence
                row["supersedes"] = [x[0] for x in con.execute("SELECT supersedes_entry_id FROM registry_relations WHERE entry_id=?", (row["id"],))]
                row["superseded_by"] = [x[0] for x in con.execute("SELECT entry_id FROM registry_relations WHERE supersedes_entry_id=?", (row["id"],))]
                row["stale"] = bool(row["stale"] )
            run_sql = "SELECT * FROM registry_runs WHERE project_id=?"
            run_params: list[Any] = [pid]
            if topic is not None:
                run_sql += " AND topic_id=?"; run_params.append(topic or "")
            run_sql += " ORDER BY id DESC LIMIT 1"
            last_run = con.execute(run_sql, run_params).fetchone()
        counts = {"total": len(rows), "candidate": 0, "confirmed": 0, "edited": 0, "excluded": 0, "stale": 0}
        for r in rows:
            counts[r["review_status"]] = counts.get(r["review_status"], 0) + 1
            if r["stale"]:
                counts["stale"] += 1
        return {"entries": rows, "counts": counts, "last_run": dict(last_run) if last_run else None}

    def registry_history(self, pid: str, entry_id: int) -> list[dict]:
        self.require_project(pid)
        if type(entry_id) is not int or entry_id <= 0:
            raise ValueError("Invalid registry entry.")
        with closing(self.connect()) as con:
            owner = con.execute("SELECT id FROM registry_entries WHERE project_id=? AND id=?", (pid, entry_id)).fetchone()
            if owner is None:
                raise ValueError("Unknown registry entry.")
            return [{"id": r["id"], "changed_at": r["changed_at"], "action": r["action"],
                     "snapshot": json.loads(r["snapshot_json"])}
                    for r in con.execute("SELECT * FROM registry_history WHERE entry_id=? ORDER BY id", (entry_id,))]

    def registry_action(self, pid: str, entry_id: int, action: str, patch: dict | None = None) -> dict:
        self.require_project(pid)
        if type(entry_id) is not int or entry_id <= 0:
            raise ValueError("Invalid registry entry.")
        if action not in {"confirm", "exclude", "complete", "edit", "move"}:
            raise ValueError("Unknown registry action.")
        now = utc_now()
        with closing(self.connect()) as con, con:
            row = con.execute("SELECT * FROM registry_entries WHERE project_id=? AND id=?", (pid, entry_id)).fetchone()
            if row is None:
                raise ValueError("Unknown registry entry.")
            if action == "confirm":
                con.execute("UPDATE registry_entries SET review_status='confirmed',stale=0,updated_at=? WHERE id=?", (now, entry_id))
            elif action == "exclude":
                con.execute("UPDATE registry_entries SET review_status='excluded',updated_at=? WHERE id=?", (now, entry_id))
            elif action == "complete":
                con.execute("UPDATE registry_entries SET lifecycle='completed',review_status='confirmed',origin='manager',stale=0,updated_at=? WHERE id=?", (now, entry_id))
            elif action == "move":
                if not isinstance(patch, dict) or set(patch) != {"lifecycle"}:
                    raise ValueError("Kanban move requires lifecycle.")
                lifecycle = patch.get("lifecycle")
                if lifecycle not in {"open", "in_progress", "blocked", "completed"}:
                    raise ValueError("Invalid Kanban column.")
                con.execute("UPDATE registry_entries SET lifecycle=?,review_status='confirmed',origin='manager',stale=0,updated_at=? WHERE id=?",
                            (lifecycle, now, entry_id))
            else:
                if not isinstance(patch, dict):
                    raise ValueError("Edit requires fields.")
                allowed = {"kind", "subject", "statement", "responsible", "deadline_text", "deadline_iso", "lifecycle"}
                if not patch or not set(patch) <= allowed:
                    raise ValueError("Invalid editable registry fields.")
                data = dict(row)
                data.update(patch)
                if data["kind"] not in REGISTRY_KINDS or data["lifecycle"] not in REGISTRY_LIFECYCLES:
                    raise ValueError("Invalid registry type/status.")
                required_string(data["subject"], "registry subject", 160)
                required_string(data["statement"], "registry statement", 700)
                for name, limit in (("responsible", 160), ("deadline_text", 200), ("deadline_iso", 40)):
                    value = data.get(name)
                    if value not in (None, ""):
                        required_string(value, name, limit)
                con.execute("""UPDATE registry_entries SET kind=?,subject=?,statement=?,responsible=?,deadline_text=?,deadline_iso=?,
                    lifecycle=?,review_status='edited',origin='manager',stale=0,updated_at=? WHERE id=?""",
                    (data["kind"], data["subject"].strip(), data["statement"].strip(),
                     (data.get("responsible") or "").strip() or None, (data.get("deadline_text") or "").strip() or None,
                     (data.get("deadline_iso") or "").strip() or None, data["lifecycle"], now, entry_id))
            updated = con.execute("SELECT * FROM registry_entries WHERE id=?", (entry_id,)).fetchone()
            snap = self._registry_snapshot(updated)
            con.execute("INSERT INTO registry_history(entry_id,changed_at,action,snapshot_json) VALUES (?,?,?,?)",
                        (entry_id, now, action, canonical(snap)))
        return snap

    def participants(self, pid: str) -> list[dict]:
        """Return human-friendly project identities without changing imported JSON."""
        self.require_project(pid)
        counts: dict[str, int] = {}
        channels: dict[str, set[str]] = {}
        source_types: dict[str, set[str]] = {}
        examples: dict[str, list[dict]] = {}
        for msg in self.current(pid):
            raw = msg["raw"]
            speaker = str(raw.get("speaker") or "").strip()
            if not speaker:
                continue
            counts[speaker] = counts.get(speaker, 0) + 1
            channel = str(raw.get("channel") or msg["source_type"] or "").strip()
            if channel:
                channels.setdefault(speaker, set()).add(channel)
            source_types.setdefault(speaker, set()).add(str(msg["source_type"] or ""))
            sample = {
                "text": str(raw.get("text") or "")[:700],
                "source_type": str(msg["source_type"] or ""),
                "channel": str(raw.get("channel") or ""),
                "occurred_at": raw.get("occurred_at"),
                "date_text": raw.get("date_text"),
                "clock_time": raw.get("clock_time"),
            }
            examples.setdefault(speaker, []).append(sample)
        with closing(self.connect()) as con:
            stored = {r["speaker"]: dict(r) for r in con.execute(
                "SELECT speaker,side,source,confidence,confirmed,updated_at FROM participant_roles WHERE project_id=?",
                (pid,))}
        result = []
        for speaker in sorted(counts, key=lambda x: x.casefold()):
            row = stored.get(speaker)
            structural_side, structural_source = structural_participant_side(speaker, source_types.get(speaker, set()))
            # A manager decision always wins. Otherwise explicit/structural evidence is
            # stronger than an older unconfirmed AI suggestion.
            if row is not None and (row["source"] == "manager" or bool(row["confirmed"])):
                side = row["side"]
                source = row["source"]
                confidence = int(row["confidence"])
                confirmed = bool(row["confirmed"])
                updated_at = row["updated_at"]
            elif structural_side is not None:
                side = structural_side
                source = structural_source or "structural"
                confidence = 100
                confirmed = True
                updated_at = None
            elif row is not None:
                side = row["side"]
                source = row["source"]
                confidence = int(row["confidence"])
                confirmed = bool(row["confirmed"])
                updated_at = row["updated_at"]
            else:
                side = "unknown"
                source = "none"
                confidence = 0
                confirmed = False
                updated_at = None
            all_examples = examples.get(speaker, [])
            shown_examples = all_examples if len(all_examples) <= 3 else [all_examples[0], all_examples[-2], all_examples[-1]]
            result.append({
                "speaker": speaker, "side": side, "source": source,
                "confidence": confidence, "confirmed": confirmed,
                "message_count": counts[speaker],
                "channels": sorted(channels.get(speaker, set())),
                "source_types": sorted(x for x in source_types.get(speaker, set()) if x),
                "identity_kind": participant_identity_kind(speaker),
                "examples": shown_examples,
                "updated_at": updated_at,
            })
        return result

    def participant_role_map(self, pid: str, *, trusted_only: bool = False) -> dict[str, dict]:
        roles = {}
        for item in self.participants(pid):
            if trusted_only and not item["confirmed"]:
                continue
            roles[item["speaker"]] = item
        return roles

    def set_participant_role(self, pid: str, speaker: str, side: str) -> dict:
        self.require_project(pid)
        required_string(speaker, "participant speaker", 250)
        if side not in PARTICIPANT_SIDES:
            raise ValueError("Unknown participant side.")
        known = {p["speaker"] for p in self.participants(pid)}
        if speaker not in known:
            raise ValueError("Unknown project participant.")
        now = utc_now()
        with closing(self.connect()) as con, con:
            con.execute("""INSERT INTO participant_roles(project_id,speaker,side,source,confidence,confirmed,updated_at)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(project_id,speaker) DO UPDATE SET side=excluded.side,source='manager',
                    confidence=100,confirmed=1,updated_at=excluded.updated_at""",
                (pid, speaker, side, "manager", 100, 1, now))
        return next(p for p in self.participants(pid) if p["speaker"] == speaker)

    def save_participant_suggestions(self, pid: str, suggestions: list[dict]) -> list[dict]:
        """Save AI suggestions only for roles not already confirmed by a manager/explicit label."""
        self.require_project(pid)
        known = {p["speaker"] for p in self.participants(pid)}
        now = utc_now()
        with closing(self.connect()) as con, con:
            for item in suggestions:
                if not isinstance(item, dict):
                    continue
                speaker = item.get("speaker")
                side = item.get("side")
                confidence = item.get("confidence")
                if speaker not in known or side not in PARTICIPANT_SIDES or type(confidence) is not int:
                    continue
                confidence = min(100, max(0, confidence))
                if explicit_participant_side(speaker) is not None:
                    continue
                current = con.execute("SELECT source,confirmed FROM participant_roles WHERE project_id=? AND speaker=?",
                                      (pid, speaker)).fetchone()
                if current is not None and (current["source"] == "manager" or current["confirmed"]):
                    continue
                con.execute("""INSERT INTO participant_roles(project_id,speaker,side,source,confidence,confirmed,updated_at)
                    VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(project_id,speaker) DO UPDATE SET side=excluded.side,source='ai',
                        confidence=excluded.confidence,confirmed=0,updated_at=excluded.updated_at""",
                    (pid, speaker, side, "ai", confidence, 0, now))
        return self.participants(pid)

    def answers(self, pid: str) -> list[dict]:
        self.require_project(pid)
        with closing(self.connect()) as con:
            return [{"id": r["id"], "created_at": r["created_at"], "question": r["question"],
                     "response": json.loads(r["response_json"])}
                    for r in con.execute("SELECT * FROM answers WHERE project_id=? ORDER BY id DESC LIMIT 12", (pid,))]
