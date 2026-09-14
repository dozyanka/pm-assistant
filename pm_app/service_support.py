"""Evidence-first answers over a complete bounded scope; search is separate."""
from __future__ import annotations
import json
import math
import re
import unicodedata
import time
from collections import defaultdict
from pathlib import Path
from typing import Callable
from . import __version__
from .db import Store, canonical, digest, parse_jsonl, utc_now
from .ollama import CHAT_MODEL, EMBED_MODEL, OllamaClient, OllamaError
from .answer_policy import wording_issues, infer_claim_kind, verifier_recheck_allowed
from .registry import (REGISTRY_EXTRACT_SCHEMA, REGISTRY_VERIFY_SCHEMA, SYSTEM_REGISTRY_EXTRACT,
                       SYSTEM_REGISTRY_VERIFY, validate_registry_extract, filter_relations,
                       registry_candidate_for_verify, merge_registry_candidates)
from .texts import (SYSTEM_QA, SYSTEM_VERIFY, SYSTEM_VERIFY_MODALITY_RECHECK, SYSTEM_LOCATE, SYSTEM_RECOVER, SYSTEM_REPAIR, SYSTEM_CONSISTENCY_REPAIR,
                    SYSTEM_POST_MEETING, SYSTEM_POST_MEETING_VERIFY, SYSTEM_PARTICIPANT_ROLES,
                    SUMMARY_QUESTION, NOT_FOUND, NOT_VERIFIED, EVIDENCE_ONLY,
                    UNKNOWN_DATES, DRAFT_NOTICE)

ANSWER_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "status": {"type": "string", "enum": ["supported", "partial", "not_found", "conflict"]},
        "claims": {"type": "array", "maxItems": 8, "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "text": {"type": "string", "maxLength": 450},
                "evidence": {"type": "array", "minItems": 1, "maxItems": 5, "items": {
                    "type": "object", "additionalProperties": False,
                    "properties": {"id": {"type": "string"}, "quote": {"type": "string", "maxLength": 600}},
                    "required": ["id", "quote"]}}
            }, "required": ["text", "evidence"]}},
        "uncertainties": {"type": "array", "maxItems": 3, "items": {"type": "string", "maxLength": 300}}
    }, "required": ["status", "claims", "uncertainties"]
}
VERIFY_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"checks": {"type": "array", "maxItems": 8, "items": {
        "type": "object", "additionalProperties": False,
        "properties": {"claim_number": {"type": "integer"},
                       "verdict": {"type": "string", "enum": ["supported", "unsupported", "uncertain"]}},
        "required": ["claim_number", "verdict"]}}}, "required": ["checks"]
}

POST_MEETING_SECTIONS = ("key_results", "client_actions", "our_actions", "fixed", "clarify")
POST_MEETING_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {section: {"type": "array", "maxItems": 6, "items": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "text": {"type": "string", "maxLength": 360},
            "evidence": {"type": "array", "minItems": 1, "maxItems": 4, "items": {
                "type": "object", "additionalProperties": False,
                "properties": {"id": {"type": "string"}, "quote": {"type": "string", "maxLength": 600}},
                "required": ["id", "quote"]}}
        }, "required": ["text", "evidence"]}} for section in POST_MEETING_SECTIONS},
    "required": list(POST_MEETING_SECTIONS)
}
POST_MEETING_VERIFY_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"checks": {"type": "array", "maxItems": 8, "items": {
        "type": "object", "additionalProperties": False,
        "properties": {"claim_number": {"type": "integer"},
                       "verdict": {"type": "string", "enum": ["supported", "unsupported", "uncertain"]}},
        "required": ["claim_number", "verdict"]}}}, "required": ["checks"]
}

PARTICIPANT_SIDES = ("our_team", "client", "contractor", "other", "unknown")


def participant_role_schema(speakers: list[str]) -> dict:
    return {
        "type": "object", "additionalProperties": False,
        "properties": {"participants": {"type": "array", "maxItems": len(speakers), "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "speaker": {"type": "string", "enum": speakers},
                "side": {"type": "string", "enum": list(PARTICIPANT_SIDES)},
                "confidence": {"type": "integer", "minimum": 0, "maximum": 100}
            },
            "required": ["speaker", "side", "confidence"]
        }}},
        "required": ["participants"]
    }


# Ordering also appears in the schema embedded into the model prompt.
ANSWER_SCHEMA["properties"] = {
    key: ANSWER_SCHEMA["properties"][key] for key in ("claims", "uncertainties", "status")
}


def locate_schema(labels: list[str]) -> dict:
    return {
        "type": "object", "additionalProperties": False,
        "properties": {"message_ids": {
            "type": "array", "maxItems": 12, "uniqueItems": True,
            "items": {"type": "string", "enum": labels}}},
        "required": ["message_ids"]
    }


def validate_locations(data: dict, allowed: set[str]) -> list[str]:
    """Selections are retrieval hints, not validated factual claims."""
    if not isinstance(data, dict) or set(data) != {"message_ids"}:
        raise OllamaError("Evidence lookup returned invalid fields. No answer was inferred.")
    ids = data["message_ids"]
    if not isinstance(ids, list) or len(ids) > 12:
        raise OllamaError("Invalid evidence lookup list.")
    if any(not isinstance(mid, str) or mid not in allowed for mid in ids):
        raise OllamaError("Evidence lookup cited a message outside its source window.")
    if len(set(ids)) != len(ids):
        raise OllamaError("Evidence lookup repeated a message ID.")
    return ids


def recall_windows(messages: list[dict]) -> list[list[dict]]:
    """Cover every selected original; overlap preserves short replies.
    Each window belongs to one source. Oversized single messages are not cut.
    The full-scope limit in ask() still applies.
    """
    groups = defaultdict(list)
    for msg in messages:
        groups[(msg["source_id"], msg["raw"].get("topic_id"))].append(msg)
    windows = []
    for group in groups.values():
        start = 0
        while start < len(group):
            block = []
            for msg in group[start:start + 12]:
                candidate = block + [msg]
                if block and len(format_context(candidate)) > 2200:
                    break
                block = candidate
            windows.append(block)
            if start + len(block) >= len(group):
                break
            start += max(1, len(block) - 2)
    return windows


def candidate_context(messages: list[dict], selected: set[str]) -> list[dict]:
    """Show unchanged originals and neighbours, never a generated fallback answer."""
    groups = defaultdict(list)
    for msg in messages:
        groups[(msg["source_id"], msg["raw"].get("topic_id"))].append(msg)
    kept = set()
    for group in groups.values():
        for i, msg in enumerate(group):
            if msg["label"] in selected:
                kept.update(m["label"] for m in group[max(0, i - 2):i + 3])
    return [
        {**source_card(msg), "selected_by_lookup": msg["label"] in selected}
        for msg in messages if msg["label"] in kept
    ]


def norm_quote(s: str) -> str:
    return " ".join(unicodedata.normalize("NFC", s).split())


def format_context(messages: list[dict]) -> str:
    lines = ["Each row is [label, speaker, date_or_null, clock_or_null, recording_offset_or_null, text].",
             "Rows preserve source order. Source order is NOT chronology between sources."]
    current_source = None
    for msg in messages:
        raw = msg["raw"]
        if msg["source_id"] != current_source:
            current_source = msg["source_id"]
            lines.append("SOURCE " + canonical({"id": current_source, "type": msg["source_type"], "topic": raw.get("topic_name")}))
        lines.append(json.dumps([msg["label"], raw.get("speaker"), raw.get("occurred_at"),
                                 raw.get("clock_time"), raw.get("offset_text"), raw["text"]],
                                ensure_ascii=False, separators=(",", ":")))
    return "\n".join(lines)


def validate_answer(data: dict, allowed: dict[str, dict]) -> dict:
    """Fail closed: unknown IDs or invented quotations invalidate the whole draft."""
    if not isinstance(data, dict) or set(data) != {"status", "claims", "uncertainties"}:
        raise OllamaError("Answer does not match the required fields.")
    if data["status"] not in {"supported", "partial", "not_found", "conflict"}:
        raise OllamaError("Unknown answer status.")
    claims = data["claims"]
    if not isinstance(claims, list) or len(claims) > 8:
        raise OllamaError("Invalid claim list.")
    if data["status"] == "not_found" and claims:
        raise OllamaError("Contradictory answer: not_found contains claims.")
    if data["status"] != "not_found" and not claims:
        raise OllamaError("Answer contains no evidence-backed claim.")
    for claim in claims:
        if not isinstance(claim, dict) or set(claim) != {"text", "evidence"}:
            raise OllamaError("Malformed claim.")
        if not isinstance(claim["text"], str) or not 1 <= len(claim["text"].strip()) <= 450:
            raise OllamaError("Invalid claim text.")
        evidence = claim["evidence"]
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= 5:
            raise OllamaError("A claim is missing its evidence.")
        for ev in evidence:
            if not isinstance(ev, dict) or set(ev) != {"id", "quote"}:
                raise OllamaError("Malformed evidence.")
            if not isinstance(ev["id"], str) or ev["id"] not in allowed:
                raise OllamaError("Model cited a message outside the selected source scope. Answer rejected.")
            quote = ev["quote"]
            if not isinstance(quote, str) or not 2 <= len(quote.strip()) <= 600:
                raise OllamaError("Invalid quotation.")
            if norm_quote(quote) not in norm_quote(allowed[ev["id"]]["raw"]["text"]):
                raise OllamaError("Model quotation does not match the original message. Answer rejected.")
    warnings = data["uncertainties"]
    if not isinstance(warnings, list) or len(warnings) > 3 or any(
        not isinstance(s, str) or not 1 <= len(s.strip()) <= 300 for s in warnings
    ):
        raise OllamaError("Invalid uncertainty list.")
    # A free-text explanation accompanying not_found is not treated as evidence.
    if data["status"] == "not_found":
        data["uncertainties"] = []
    return data


def answer_consistency_issue(data: object) -> str | None:
    """Identify only contradictions that can be retried without trusting their claims.

    This is deliberately narrower than validate_answer: malformed citations, unknown IDs,
    bad quotes and arbitrary fields remain hard failures.
    """
    if not isinstance(data, dict) or set(data) != {"status", "claims", "uncertainties"}:
        return None
    status, claims = data.get("status"), data.get("claims")
    if not isinstance(claims, list):
        return None
    if status == "not_found" and claims:
        return "not_found_with_claims"
    if status in {"supported", "partial", "conflict"} and not claims:
        return "answer_status_without_claims"
    return None


def verifier_verdicts(data: dict, count: int) -> dict[int, str]:
    if not isinstance(data, dict) or set(data) != {"checks"} or not isinstance(data["checks"], list):
        raise OllamaError("The verification pass returned invalid JSON fields.")
    result = {}
    for check in data["checks"]:
        if not isinstance(check, dict) or set(check) != {"claim_number", "verdict"}:
            raise OllamaError("Malformed verification result.")
        n = check["claim_number"]
        if type(n) is not int or n in result or not 1 <= n <= count:
            raise OllamaError("Missing or repeated claim in verification result.")
        if check["verdict"] not in {"supported", "unsupported", "uncertain"}:
            raise OllamaError("Unknown verification verdict.")
        result[n] = check["verdict"]
    if set(result) != set(range(1, count + 1)):
        raise OllamaError("Verification did not cover every claim. Answer rejected.")
    return result


def source_card(msg: dict) -> dict:
    return {"id": msg["label"], "message_fk": msg["message_fk"], "revision": msg["revision"],
            "message_id": msg["message_id"], "source_id": msg["source_id"],
            "source_type": msg["source_type"], "raw": msg["raw"], "imported_at": msg["imported_at"]}


def validate_post_meeting(data: dict, allowed: dict[str, dict]) -> dict:
    """Validate every post-meeting bullet against immutable source quotations."""
    if not isinstance(data, dict) or set(data) != set(POST_MEETING_SECTIONS):
        raise OllamaError("Post-meeting draft does not match the required sections.")
    for section in POST_MEETING_SECTIONS:
        items = data[section]
        if not isinstance(items, list) or len(items) > 6:
            raise OllamaError("Invalid post-meeting section size.")
        for item in items:
            if not isinstance(item, dict) or set(item) != {"text", "evidence"}:
                raise OllamaError("Malformed post-meeting item.")
            if not isinstance(item["text"], str) or not 1 <= len(item["text"].strip()) <= 360:
                raise OllamaError("Invalid post-meeting item text.")
            evidence = item["evidence"]
            if not isinstance(evidence, list) or not 1 <= len(evidence) <= 4:
                raise OllamaError("Post-meeting item is missing evidence.")
            seen = set()
            for ev in evidence:
                if not isinstance(ev, dict) or set(ev) != {"id", "quote"}:
                    raise OllamaError("Malformed post-meeting evidence.")
                mid = ev["id"]
                quote = ev["quote"]
                if not isinstance(mid, str) or mid not in allowed:
                    raise OllamaError("Post-meeting cited a message outside the selected meeting.")
                if mid in seen:
                    raise OllamaError("Post-meeting repeated the same evidence message.")
                seen.add(mid)
                if not isinstance(quote, str) or not 2 <= len(quote.strip()) <= 600:
                    raise OllamaError("Invalid post-meeting quotation.")
                if norm_quote(quote) not in norm_quote(allowed[mid]["raw"]["text"]):
                    raise OllamaError("Post-meeting quotation does not match the original meeting message.")
    return data


def meeting_date_key(msg: dict) -> str:
    raw = msg["raw"]
    date_text = raw.get("date_text")
    if isinstance(date_text, str) and date_text.strip():
        return date_text.strip()
    occurred = raw.get("occurred_at")
    if isinstance(occurred, str) and len(occurred) >= 10:
        return occurred[:10]
    return ""


def meeting_date_label(key: str) -> str:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", key or ""):
        year, month, day = key.split("-")
        return f"{day}.{month}.{year}"
    return key or "Дата не указана"


def _post_tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-zа-яё0-9]{3,}", text.casefold(), flags=re.UNICODE))


def _post_stems(text: str) -> set[str]:
    stop = {
        "этот", "эта", "это", "этого", "после", "перед", "через", "течение", "стороны",
        "сторона", "нашей", "вашей", "себя", "тогда", "будет", "будем", "нужно", "можно",
        "подтвержда", "согласн", "верно", "так", "пока",
    }
    result = set()
    for token in re.findall(r"[a-zа-яё0-9]{4,}", text.casefold().replace("ё", "е"), flags=re.UNICODE):
        stem = token[:6] if len(token) >= 7 else token
        if stem not in stop:
            result.add(stem)
    return result


def _post_similar(left: str, right: str) -> bool:
    a, b = _post_tokens(left), _post_tokens(right)
    if not a or not b:
        return norm_quote(left).casefold() == norm_quote(right).casefold()
    overlap = len(a & b) / max(1, len(a | b))
    la, lb = norm_quote(left).casefold(), norm_quote(right).casefold()
    return overlap >= 0.68 or (min(len(la), len(lb)) >= 24 and (la in lb or lb in la))


def _post_action_similar(left: str, right: str) -> bool:
    if _post_similar(left, right):
        return True
    sa, sb = _post_stems(left), _post_stems(right)
    if not sa or not sb:
        return False
    shared = len(sa & sb)
    containment = shared / max(1, min(len(sa), len(sb)))
    return shared >= 2 and containment >= 0.45


def _role_context(participants: list[dict], speakers: set[str] | None = None) -> str:
    if not participants:
        return ""
    rows = []
    for item in participants:
        if speakers is not None and item["speaker"] not in speakers:
            continue
        if item["side"] == "unknown":
            continue
        status = "verified" if item["confirmed"] else "ai_suggestion"
        rows.append({"speaker": item["speaker"], "side": item["side"], "status": status,
                     "confidence": item["confidence"]})
    return "\nPARTICIPANTS:\n" + canonical(rows) if rows else ""


def _meaningful_action_overlap(item_text: str, evidence_text: str) -> bool:
    """Conservative guard against assigning a short acknowledgement as a new action."""
    a, b = _post_stems(item_text), _post_stems(evidence_text)
    if not a or not b:
        return False
    generic = {"подтве", "соглас", "принят", "верно", "спасиб"}
    shared = {x for x in a & b if x not in generic}
    return bool(shared)


def post_meeting_action_sanity(item: dict, allowed: dict[str, dict], trusted_roles: dict[str, dict]) -> bool:
    section = item.get("section")
    if section not in {"client_actions", "our_actions"}:
        return True
    target = "client" if section == "client_actions" else "our_team"
    for ev in item.get("evidence", []):
        msg = allowed.get(ev.get("id"))
        if msg is None:
            continue
        speaker = str(msg["raw"].get("speaker") or "").strip()
        role = trusted_roles.get(speaker)
        if role and role.get("side") == target and _meaningful_action_overlap(item.get("text", ""), msg["raw"].get("text", "")):
            return True
    return False


def merge_post_meeting(items: list[dict]) -> dict[str, list[dict]]:
    """Conservative deduplication inside and across template sections."""
    result = {section: [] for section in POST_MEETING_SECTIONS}
    for item in items:
        section = item["section"]
        matcher = _post_action_similar if section in {"client_actions", "our_actions"} else _post_similar
        existing = next((x for x in result[section] if matcher(x["text"], item["text"])), None)
        if existing is None:
            result[section].append({"text": item["text"], "evidence": list(item["evidence"])})
            continue
        known = {ev["id"] for ev in existing["evidence"]}
        for ev in item["evidence"]:
            if ev["id"] not in known and len(existing["evidence"]) < 4:
                existing["evidence"].append(ev); known.add(ev["id"])
        # Prefer the more informative formulation, but keep the shorter one if it already carries a deadline.
        if len(item["text"]) > len(existing["text"]):
            existing["text"] = item["text"]
    for section in POST_MEETING_SECTIONS:
        result[section] = result[section][:6]
    return result


def fingerprint(messages: list[dict]) -> str:
    return digest([(m["id"], m["content_hash"]) for m in messages])


def make_chunks(messages: list[dict]) -> list[dict]:
    """Overlapping windows do not cross source boundaries or lose short replies."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for msg in messages:
        groups[(msg["source_id"], msg["raw"].get("topic_id"))].append(msg)
    chunks = []
    for group in groups.values():
        start = 0
        while start < len(group):
            block = []
            chars = 0
            for m in group[start:start + 6]:
                if len(m["raw"]["text"]) > 3500:
                    raise ValueError("A message exceeds the search-window limit; originals are retained, but search indexing was not completed.")
                cost = len(m["raw"]["text"]) + 100
                if block and chars + cost > 3500:
                    break
                block.append(m)
                chars += cost
            chunks.append({"revision_ids": [m["id"] for m in block], "text": format_context(block)})
            if start + len(block) == len(group):
                break
            start += max(1, len(block) - 2)
    return chunks


def registry_chunks(messages: list[dict], char_limit: int = 5600, message_limit: int = 24,
                    overlap: int = 4) -> list[list[dict]]:
    """Create complete overlapping windows for registry extraction.

    For the GigaSchool DOCX importer every row has an explicit ``source_locator.event_index``
    that represents the common communication chronology in the source document. In that case
    we may safely mix channels in the same window. For arbitrary imports without such an
    explicit sequence, windows stay inside one source so we never invent cross-source chronology.
    Every original message is covered by at least one window.
    """
    if not messages:
        return []
    if not (2000 <= char_limit <= 12000) or not (6 <= message_limit <= 40) or not (0 <= overlap < message_limit):
        raise ValueError("Invalid registry window configuration.")

    def explicit_index(msg: dict):
        locator = msg.get("raw", {}).get("source_locator")
        value = locator.get("event_index") if isinstance(locator, dict) else None
        return value if type(value) is int and value > 0 else None

    indexes = [explicit_index(m) for m in messages]
    use_global_sequence = all(x is not None for x in indexes) and len(set(indexes)) == len(indexes)
    if use_global_sequence:
        groups = [sorted(messages, key=explicit_index)]
    else:
        grouped: dict[tuple[str, str | None], list[dict]] = defaultdict(list)
        for msg in messages:
            grouped[(msg["source_id"], msg["raw"].get("topic_id"))].append(msg)
        groups = list(grouped.values())

    windows: list[list[dict]] = []
    for group in groups:
        start = 0
        while start < len(group):
            block: list[dict] = []
            for msg in group[start:start + message_limit]:
                candidate = block + [msg]
                rendered = format_context(candidate)
                if block and len(rendered) > char_limit:
                    break
                # A single long source message remains atomic. The model sees the original or we
                # fail loudly; source text is never silently truncated.
                if not block and len(rendered) > 24000:
                    raise ValueError("One source message is too large for registry extraction; the original was not truncated.")
                block = candidate
            if not block:
                raise ValueError("Could not create a complete registry extraction window.")
            windows.append(block)
            end = start + len(block)
            if end >= len(group):
                break
            start = max(start + 1, end - min(overlap, len(block) - 1))

    expected = {m["label"] for m in messages}
    covered = {m["label"] for window in windows for m in window}
    if covered != expected:
        raise ValueError("Internal registry window error: not every source message is covered.")
    return windows



# Public compatibility exports used by the service facade and regression tests.
__all__ = [name for name in globals() if not name.startswith("__") or name == "__version__"]
