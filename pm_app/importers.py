"""Safe local import adapters for JSONL, text, CSV and DOCX sources."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

from .db import MAX_ROWS, PROJECT_ID, parse_jsonl, required_string, validate_row

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
PROJECT_HEADER_RE = re.compile(r"^(\d{2})\.\s+(.+?)\s*$")
META_RE = re.compile(r"^Проект:\s*([^·]+?)\s*·\s*Тип:\s*([^·]+?)\s*·\s*Этап:\s*(.+?)\s*$")
TIME_RE = re.compile(r"^\d{2}:\d{2}$")


@dataclass
class ImportBundle:
    rows: list[dict]
    names: dict[str, str]
    file_name: str
    file_format: str
    content_hash: str
    batch_key: str | None = None
    batch_name: str | None = None
    is_dataset: bool = False

    @property
    def project_ids(self) -> list[str]:
        return sorted({r["project_id"] for r in self.rows})


def _slug(value: str, fallback: str = "source") -> str:
    value = value.lower().strip()
    translit = str.maketrans({
        "а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"e","ж":"zh","з":"z","и":"i","й":"y",
        "к":"k","л":"l","м":"m","н":"n","о":"o","п":"p","р":"r","с":"s","т":"t","у":"u","ф":"f",
        "х":"h","ц":"c","ч":"ch","ш":"sh","щ":"sch","ы":"y","э":"e","ю":"yu","я":"ya","ь":"","ъ":"",
    })
    value = value.translate(translit)
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value[:60] or fallback


def generate_project_id(name: str, existing: set[str] | None = None) -> str:
    required_string(name, "project name", 120)
    existing = set(existing or ())
    base = _slug(name, "project")[:68]
    candidate = base
    index = 2
    while candidate in existing or not PROJECT_ID.fullmatch(candidate):
        suffix = f"-{index}"
        candidate = base[:80-len(suffix)] + suffix
        index += 1
    return candidate


def _docx_paragraphs(data: bytes) -> list[str]:
    try:
        with ZipFile(io.BytesIO(data)) as zf:
            xml = zf.read("word/document.xml")
    except (BadZipFile, KeyError, OSError) as exc:
        raise ValueError("DOCX-файл повреждён или не является документом Word.") from exc
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ValueError("Не удалось прочитать структуру DOCX.") from exc
    ns = {"w": WORD_NS}
    result: list[str] = []
    for p in root.findall(".//w:body/w:p", ns):
        parts: list[str] = []
        for node in p.iter():
            tag = node.tag.rsplit("}", 1)[-1]
            if tag == "t":
                parts.append(node.text or "")
            elif tag in {"br", "cr", "tab"}:
                parts.append("\n" if tag != "tab" else "\t")
        text = "".join(parts).strip()
        if text:
            result.append(text)
    if not result:
        raise ValueError("DOCX не содержит читаемого текста.")
    return result


def _source_type(label: str) -> str:
    low = label.lower()
    if low.startswith("email"):
        return "email"
    if low.startswith("созвон"):
        return "meeting_transcript"
    if low.startswith("пачка"):
        return "internal_chat"
    if low.startswith("max") or low.startswith("telegram"):
        return "client_chat"
    return "document"


def _parse_gigaschool_large(paragraphs: list[str], file_name: str, content_hash: str) -> ImportBundle | None:
    if "Большая синтетическая база данных" not in paragraphs[:6]:
        return None
    headers = [(i, PROJECT_HEADER_RE.match(text)) for i, text in enumerate(paragraphs)]
    headers = [(i, m) for i, m in headers if m]
    if len(headers) < 10:
        return None

    rows: list[dict] = []
    names: dict[str, str] = {}
    for hindex, (start, match) in enumerate(headers):
        end = headers[hindex + 1][0] if hindex + 1 < len(headers) else len(paragraphs)
        display_name = match.group(2).strip()
        meta = None
        for text in paragraphs[start + 1:min(end, start + 7)]:
            meta = META_RE.match(text)
            if meta:
                break
        if meta is None:
            raise ValueError(f"Не удалось прочитать метаданные проекта «{display_name}».")
        source_code = meta.group(1).strip()
        project_type, project_stage = meta.group(2).strip(), meta.group(3).strip()
        pid = "giga-" + _slug(source_code, f"prj-{hindex+1:02d}")
        if not PROJECT_ID.fullmatch(pid):
            raise ValueError("Некорректный идентификатор проекта в большой синтетической базе.")
        names[pid] = display_name

        try:
            raw_start = paragraphs.index("Сырая коммуникация по проекту", start, end) + 1
        except ValueError as exc:
            raise ValueError(f"В проекте «{display_name}» не найден раздел сырой коммуникации.") from exc

        current_date: str | None = None
        occurrence: dict[tuple[str, str, str], int] = {}
        event_index = 0
        for text in paragraphs[raw_start:end]:
            if DATE_RE.fullmatch(text):
                current_date = text
                continue
            lines = [x.strip() for x in text.splitlines() if x.strip()]
            if len(lines) < 2:
                continue
            header = lines[0]
            parts = [x.strip() for x in header.split(" · ")]
            if len(parts) < 3 or not TIME_RE.fullmatch(parts[0]):
                continue
            clock = parts[0]
            speaker = parts[-1]
            source_label = " · ".join(parts[1:-1])
            body = "\n".join(lines[1:]).strip()
            if not body:
                continue
            event_index += 1
            source_slug = _slug(source_label, "source")
            date_key = current_date or "date-unknown"
            base_key = (date_key, clock, source_slug)
            occurrence[base_key] = occurrence.get(base_key, 0) + 1
            message_id = f"giga:{source_code.lower()}:{date_key}:{clock}:{source_slug}:{occurrence[base_key]}"
            row = {
                "project_id": pid,
                "source_id": f"{pid}-{source_slug}",
                "source_type": _source_type(source_label),
                "message_id": message_id,
                "speaker": speaker,
                "text": body,
                "occurred_at": None,
                "timezone": None,
                "clock_time": clock,
                "date_text": current_date,
                "channel": source_label,
                "project_code": source_code,
                "project_type": project_type,
                "project_stage": project_stage,
                "source_locator": {"file": file_name, "project": source_code, "event_index": event_index},
            }
            validate_row(row)
            rows.append(row)
            if len(rows) > MAX_ROWS:
                raise ValueError(f"В одном импорте поддерживается не более {MAX_ROWS} сообщений.")

    # This known company fixture declares exact coverage. Reject partial parsing rather than silently losing rows.
    if len(names) == 15 and len(rows) != 1710:
        raise ValueError(f"Большая синтетическая база распознана не полностью: найдено {len(rows)} из 1710 событий.")
    if len(names) != 15:
        raise ValueError(f"Ожидалось 15 проектов в большой синтетической базе, найдено {len(names)}.")
    return ImportBundle(rows=rows, names=names, file_name=file_name, file_format="docx",
                        content_hash=content_hash, batch_key="gigaschool-large-synthetic-base",
                        batch_name="Большая синтетическая база", is_dataset=True)


def _generic_rows(texts: list[str], file_name: str, pid: str, source_type: str) -> list[dict]:
    source_id = f"import-{_slug(Path(file_name).stem, 'source')}"
    rows = []
    for i, text in enumerate(texts, 1):
        text = text.strip()
        if not text:
            continue
        row = {"project_id": pid, "source_id": source_id, "source_type": source_type,
               "message_id": f"{source_id}:p{i:05d}", "speaker": "Импорт",
               "text": text, "occurred_at": None, "timezone": None,
               "source_locator": {"file": file_name, "paragraph": i}}
        validate_row(row)
        rows.append(row)
    if not rows:
        raise ValueError("В файле нет непустых сообщений для импорта.")
    if len(rows) > MAX_ROWS:
        raise ValueError(f"В одном импорте поддерживается не более {MAX_ROWS} сообщений.")
    return rows


def parse_upload(file_name: str, data: bytes, selected_project: str | None = None) -> ImportBundle:
    required_string(file_name, "file name", 260)
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ValueError("Файл пуст.")
    if len(data) > 8 * 1024 * 1024:
        raise ValueError("Размер импортируемого файла не должен превышать 8 МиБ.")
    data = bytes(data)
    ext = Path(file_name).suffix.lower()
    content_hash = hashlib.sha256(data).hexdigest()

    if ext in {".jsonl", ".ndjson", ".json"}:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("JSONL должен быть в UTF-8.") from exc
        rows = parse_jsonl(text)
        if selected_project is not None:
            for row in rows:
                if row["project_id"] != selected_project:
                    raise ValueError("JSONL содержит сообщения другого проекта.")
        return ImportBundle(rows=rows, names={}, file_name=file_name, file_format="jsonl", content_hash=content_hash)

    if ext == ".docx":
        paragraphs = _docx_paragraphs(data)
        large = _parse_gigaschool_large(paragraphs, file_name, content_hash)
        if large:
            return large
        if not selected_project:
            raise ValueError("Для обычного DOCX сначала выберите или создайте проект.")
        return ImportBundle(rows=_generic_rows(paragraphs, file_name, selected_project, "document"), names={},
                            file_name=file_name, file_format="docx", content_hash=content_hash)

    if ext in {".txt", ".md"}:
        if not selected_project:
            raise ValueError("Для текстового файла сначала выберите или создайте проект.")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("Текстовый файл должен быть в UTF-8.") from exc
        paragraphs = [x.strip() for x in re.split(r"\n\s*\n|\r?\n", text) if x.strip()]
        return ImportBundle(rows=_generic_rows(paragraphs, file_name, selected_project, "text"), names={},
                            file_name=file_name, file_format="txt", content_hash=content_hash)

    if ext == ".csv":
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("CSV должен быть в UTF-8.") from exc
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames or "text" not in reader.fieldnames:
            raise ValueError("CSV должен содержать колонку text.")
        rows = []
        source_default = f"import-{_slug(Path(file_name).stem, 'csv')}"
        for i, item in enumerate(reader, 1):
            pid = (item.get("project_id") or selected_project or "").strip()
            if not pid:
                raise ValueError("CSV без project_id можно импортировать только в выбранный проект.")
            row = {k: v for k, v in item.items() if v not in (None, "")}
            row["project_id"] = pid
            row.setdefault("source_id", source_default)
            row.setdefault("source_type", "table")
            row.setdefault("message_id", f"{source_default}:r{i:05d}")
            row.setdefault("speaker", "Импорт")
            row.setdefault("occurred_at", None)
            row.setdefault("timezone", None)
            validate_row(row)
            rows.append(row)
        if not rows:
            raise ValueError("CSV не содержит строк для импорта.")
        return ImportBundle(rows=rows, names={}, file_name=file_name, file_format="csv", content_hash=content_hash)

    raise ValueError("Поддерживаются JSONL/NDJSON, TXT/MD, CSV и DOCX.")
