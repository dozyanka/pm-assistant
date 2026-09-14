"""Conservative wording guards, not a substitute for semantic verification.

No project-specific answers or regex-based rewriting of factual statements.
Unsafe wording gets one model repair, then is removed rather than published.
"""
from __future__ import annotations
import re
from datetime import datetime, timedelta

RELATIVE = re.compile(r"\b(?:сегодня|завтра|послезавтра|на этой неделе|до конца недели|в следующий|на следующей|до пятниц|до четверг|до сред)\w*", re.I)
ANCHOR = re.compile(r"относительно|после (?:исходн\w+ |этого |того )?(?:сообщени|встреч|созвон)|(?:дата|даты|день|дня) (?:исходн\w+ |этого |той |того )?(?:сообщени|встреч|созвон)|календарн\w+ (?:дат|срок)", re.I)
PROMISE = re.compile(r"обещ\w*|планир\w*|пришл[юеё]\w*|передам|подготовим|проверим|верн[её]мся|запрошу|посмотрю|начн[её]м", re.I)
ATTRIBUTED = re.compile(r"обещ\w*|планир\w*|намерен\w*|сообщил\w*|по словам|ожида\w*|согласован\w*|зафиксирован\w*|договорил\w*", re.I)
CATEGORICAL_FUTURE = re.compile(r"\b(?:будет|будут|передаст|пришл[её]т|подготовит|завершит|проверит|предоставит)\b", re.I)
NEGATIVE_STATUS = re.compile(r"\bне\s+(?:реализован\w*|выполнен\w*|готов\w*|передан\w*|получен\w*|завершен\w*|завершён\w*)", re.I)
FULL_DATE = re.compile(r"\b\d{1,2}[./-]\d{1,2}[./-]\d{4}\b|\b20\d{2}\b")

def calendar_tokens(raw: dict) -> set[str]:
    """Known source-local dates and explicitly anchored today/tomorrow only.
    No current date, import time or guessed year is ever used.
    This permits a correct format conversion without treating it as fabrication.
    """
    dates = set()
    original = raw['text']
    for item in re.findall(r'\b\d{1,2}[./-]\d{1,2}[./-]\d{4}\b', original):
        try:
            day, month, year = map(int, re.split(r'[./-]', item))
            dates.add(datetime(year, month, day).date())
        except ValueError:
            pass
    stamp = raw.get('occurred_at')
    if stamp:
        try:
            base = datetime.fromisoformat(stamp.replace('Z','+00:00')).date()
            dates.add(base)
            for pattern,delta in ((r'\bсегодня\b',0),(r'\bзавтра\b',1),(r'\bпослезавтра\b',2)):
                if re.search(pattern,original,re.I):
                    dates.add(base+timedelta(days=delta))
        except (ValueError,OverflowError):
            pass
    result=set()
    for date in dates:
        result.add(str(date.year))
        for separator in ('.','/','-'):
            result.add(f'{date.day:02d}{separator}{date.month:02d}{separator}{date.year}')
            result.add(f'{date.day}{separator}{date.month}{separator}{date.year}')
    return result


CLAIM_KINDS = {"FACT", "PROMISE", "PLAN", "DECISION", "REQUIREMENT", "QUESTION", "COMPLETION", "UNKNOWN"}

_KIND_UNKNOWN = re.compile(r"нет\s+подтверждени\w*|неизвест\w*|неясн\w*|не\s+найден\w*\s+(?:подтверждени|данн|сведени)", re.I)
_KIND_PROMISE = re.compile(r"обещ\w*|обязал\w*\s+(?:ся|ись)|верн[её]мся\s+с\s+ответом", re.I)
_KIND_PLAN = re.compile(r"планир\w*|намерен\w*|намерен[аы]?|собира\w*|план\s+(?:сделать|передать|проверить|подготовить)", re.I)
_KIND_DECISION = re.compile(r"согласован\w*|решил\w*|договорил\w*|зафиксирован\w*\s+(?:решени|вариант)|оставляем|выбран\w*\s+(?:вариант|канал|способ)", re.I)
_KIND_REQUIREMENT = re.compile(r"требован\w*|нужно\s+(?:сделать|реализовать|добавить|проверить)|необходим\w*|должен\w*", re.I)
_KIND_COMPLETION = re.compile(r"(?:уже\s+)?(?:реализован\w*|выполнен\w*|готов\w*|заверш[её]н\w*|получен\w*|передан\w*|сделан\w*|внедр[её]н\w*)", re.I)
_KIND_QUESTION = re.compile(r"\?$|(?:вопрос|нужно\s+уточнить|требует\s+уточнения)", re.I)

def infer_claim_kind(claim: dict, allowed: dict[str, dict]) -> str:
    """Classify the proposition for the verifier without changing the published answer.

    This is intentionally conservative. The kind is only a verification hint; it never
    turns an unsupported claim into a supported one and is not stored as a project fact.
    """
    text = str(claim.get("text") or "")
    # Modality of the main proposition wins over an uncertainty clause appended
    # to the same sentence (e.g. "Client promised ...; calendar date is unknown").
    if _KIND_PROMISE.search(text):
        return "PROMISE"
    if _KIND_PLAN.search(text):
        return "PLAN"
    if _KIND_DECISION.search(text):
        return "DECISION"
    if _KIND_REQUIREMENT.search(text):
        return "REQUIREMENT"
    if _KIND_COMPLETION.search(text):
        return "COMPLETION"
    if _KIND_UNKNOWN.search(text):
        return "UNKNOWN"
    if _KIND_QUESTION.search(text):
        return "QUESTION"

    # If a safe repaired sentence says only "Клиент сообщил...", inspect the cited
    # originals to distinguish an explicit first-person commitment from a plain fact.
    originals = []
    for ev in claim.get("evidence", []):
        msg = allowed.get(ev.get("id"))
        if msg:
            originals.append(str(msg["raw"].get("text") or ""))
    source_text = "\n".join(originals)
    if re.search(r"\b(?:пришлю|передам|подготовим|проверим|верн[её]мся|сделаем|добавим|отправлю|предоставлю)\b", source_text, re.I):
        if re.search(r"сообщил\w*|по\s+словам|заявил\w*", text, re.I):
            return "PROMISE"
    return "FACT"


def verifier_recheck_allowed(kind: str) -> bool:
    """Only speech-act / agreement kinds get a second semantic verification pass.

    Completion and generic fact claims stay fail-closed after one rejection because they
    are the highest-risk classes for accidental overstatement.
    """
    return kind in {"PROMISE", "PLAN", "DECISION", "REQUIREMENT"}


def wording_issues(claims: list[dict], allowed: dict[str, dict]) -> list[dict]:
    """Return technical reason codes only; do not manufacture a replacement fact."""
    problems = []
    for number, claim in enumerate(claims, 1):
        text = claim['text']
        originals = [allowed[e['id']]['raw'] for e in claim['evidence']]
        original_text = '\n'.join(r['text'] for r in originals)
        reasons = []
        if RELATIVE.search(text) and not ANCHOR.search(text):
            reasons.append('relative_deadline_needs_source_anchor')
        if (CATEGORICAL_FUTURE.search(text) and PROMISE.search(original_text)
                and not ATTRIBUTED.search(text)):
            reasons.append('promise_is_not_a_guaranteed_event')
        if NEGATIVE_STATUS.search(text) and not NEGATIVE_STATUS.search(original_text):
            reasons.append('missing_confirmation_is_not_negative_status')
        date_evidence = original_text + '\n' + '\n'.join(str(r.get('occurred_at') or '') for r in originals)
        supported_dates = set().union(*(calendar_tokens(r) for r in originals))
        if any(date not in date_evidence and date not in supported_dates for date in FULL_DATE.findall(text)):
            reasons.append('calendar_date_not_supported_by_cited_sources')
        if reasons:
            problems.append({'claim_number': number, 'reasons': reasons})
    return problems