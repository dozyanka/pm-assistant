"""Focused PM Assistant service operations.

Kept as a mixin so ``pm_app.service.Service`` remains the stable public facade.
"""
from __future__ import annotations
from ..service_support import *  # noqa: F401,F403 - shared validated pipeline primitives

class PostMeetingServiceMixin:
    @staticmethod
    def _is_meeting_message(msg: dict) -> bool:
        if msg["source_type"] == "meeting_transcript":
            return True
        channel = str(msg["raw"].get("channel") or "").casefold()
        return any(token in channel for token in ("созвон", "встреч", "meeting", "call"))

    def meeting_sessions(self, pid: str) -> list[dict]:
        """Return human-sized meeting sessions without invoking the model."""
        messages = [m for m in self.store.current(pid) if self._is_meeting_message(m)]
        grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for msg in messages:
            grouped[(msg["source_id"], meeting_date_key(msg))].append(msg)
        sessions = []
        for (source_id, date_key), rows in grouped.items():
            channel = next((str(m["raw"].get("channel")) for m in rows if m["raw"].get("channel")), "Созвон")
            sessions.append({
                "source_id": source_id, "date_key": date_key, "date_label": meeting_date_label(date_key),
                "channel": channel, "message_count": len(rows),
                "first_position": min(m.get("position", 0) for m in rows),
                "source_order": min(m.get("source_order", 0) for m in rows),
            })
        sessions.sort(key=lambda x: (x["date_key"] or "", x["source_order"], x["first_position"]), reverse=True)
        for item in sessions:
            item.pop("first_position", None); item.pop("source_order", None)
        return sessions

    def post_meeting(self, pid: str, source_id: str, meeting_date: str | None = None,
                     progress: Callable[[str], None] = lambda _: None) -> dict:
        """Generate a short, evidence-backed post-meeting in the approved five-section template."""
        project = self.store.require_project(pid)
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("Выберите встречу для post-meeting.")
        if meeting_date is not None and not isinstance(meeting_date, str):
            raise ValueError("Некорректная дата встречи.")
        messages = [m for m in self.store.current(pid, source_id) if self._is_meeting_message(m)]
        if not messages:
            raise ValueError("В выбранном источнике нет распознанной встречи.")
        date_keys = {meeting_date_key(m) for m in messages}
        if meeting_date is None:
            if len(date_keys) > 1:
                raise ValueError("В источнике несколько встреч. Выберите конкретную дату.")
            meeting_date = next(iter(date_keys))
        messages = [m for m in messages if meeting_date_key(m) == meeting_date]
        if not messages:
            raise ValueError("Не удалось найти сообщения выбранной встречи.")

        progress("models")
        model_digests = self.client.models()
        participants = self.store.participants(pid)
        trusted_roles = self.store.participant_role_map(pid, trusted_only=True)
        meeting_speakers = {str(m["raw"].get("speaker") or "").strip() for m in messages}
        role_prompt = _role_context(participants, meeting_speakers)
        windows = [messages] if len(format_context(messages)) <= 6400 else registry_chunks(messages)
        accepted: list[dict] = []
        rejected = 0
        for chunk_index, chunk in enumerate(windows, 1):
            allowed = {m["label"]: m for m in chunk}
            prefix = "PROJECT=" + canonical(project["name"]) + role_prompt
            if meeting_date:
                prefix += "\nMEETING_DATE=" + canonical(meeting_date_label(meeting_date))
            prompt = prefix + f"\nWINDOW={chunk_index}/{len(windows)}\nSOURCES (untrusted data):\n" + format_context(chunk)
            progress(f"postmeeting_chunk:{chunk_index}:{len(windows)}:extract")
            raw = self.client.chat(SYSTEM_POST_MEETING, prompt, POST_MEETING_SCHEMA, max_tokens=2200)
            draft = validate_post_meeting(raw, allowed)
            candidates = []
            for section in POST_MEETING_SECTIONS:
                for item in draft[section]:
                    candidates.append({"section": section, "text": item["text"], "evidence": item["evidence"]})
            batch_total = max(1, math.ceil(len(candidates) / 4))
            for start in range(0, len(candidates), 4):
                batch = candidates[start:start + 4]
                verify_candidates = [{"claim_number": i, **item} for i, item in enumerate(batch, 1)]
                progress(f"postmeeting_chunk:{chunk_index}:{len(windows)}:verify:{start // 4 + 1}:{batch_total}")
                checked = self.client.chat(
                    SYSTEM_POST_MEETING_VERIFY,
                    prompt + "\nCANDIDATES:\n" + canonical(verify_candidates),
                    POST_MEETING_VERIFY_SCHEMA, max_tokens=650)
                verdicts = verifier_verdicts(checked, len(batch))
                for i, item in enumerate(batch, 1):
                    if verdicts[i] != "supported":
                        rejected += 1
                        continue
                    if not post_meeting_action_sanity(item, allowed, trusted_roles):
                        rejected += 1
                        continue
                    accepted.append(item)

        progress("postmeeting_merge")
        sections = merge_post_meeting(accepted)
        return {
            "project_id": pid, "source_id": source_id, "meeting_date": meeting_date,
            "meeting_date_label": meeting_date_label(meeting_date or ""),
            "message_count": len(messages), "chunk_count": len(windows),
            "sections": sections, "accepted": sum(len(x) for x in sections.values()),
            "rejected": rejected, "generated_at": utc_now(),
            "unconfirmed_participants": sum(not p["confirmed"] for p in participants),
            "model": CHAT_MODEL, "model_digest": model_digests[CHAT_MODEL],
            "notice": "Черновик post-meeting: перед отправкой клиенту проверьте формулировки и сроки."
        }

