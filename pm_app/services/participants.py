"""Focused PM Assistant service operations.

Kept as a mixin so ``pm_app.service.Service`` remains the stable public facade.
"""
from __future__ import annotations
from ..service_support import *  # noqa: F401,F403 - shared validated pipeline primitives

class ParticipantServiceMixin:
    @staticmethod
    def _participant_examples(messages: list[dict], speakers: list[str]) -> list[dict]:
        """Build compact conversational examples for role inference; originals stay unchanged."""
        by_source: dict[str, list[dict]] = defaultdict(list)
        for msg in messages:
            by_source[msg["source_id"]].append(msg)
        index: dict[str, list[tuple[list[dict], int]]] = defaultdict(list)
        for group in by_source.values():
            group.sort(key=lambda m: (m.get("position", 0), m["id"]))
            for i, msg in enumerate(group):
                speaker = str(msg["raw"].get("speaker") or "").strip()
                if speaker in speakers:
                    index[speaker].append((group, i))
        out = []
        for speaker in speakers:
            refs = index.get(speaker, [])
            if len(refs) > 4:
                refs = refs[:2] + refs[-2:]
            examples = []
            for group, i in refs:
                msg = group[i]
                def compact(other: dict | None):
                    if other is None:
                        return None
                    raw = other["raw"]
                    return {"speaker": str(raw.get("speaker") or ""),
                            "text": str(raw.get("text") or "")[:320]}
                examples.append({
                    "source_type": msg["source_type"],
                    "channel": str(msg["raw"].get("channel") or ""),
                    "before": compact(group[i - 1] if i > 0 else None),
                    "message": compact(msg),
                    "after": compact(group[i + 1] if i + 1 < len(group) else None),
                })
            out.append({"speaker": speaker, "examples": examples})
        return out

    def infer_participant_roles(self, pid: str,
                                progress: Callable[[str], None] = lambda _: None) -> dict:
        """Suggest participant sides from project context without changing imported JSON."""
        self.store.require_project(pid)
        participants = self.store.participants(pid)
        speakers = [p["speaker"] for p in participants if not p["confirmed"]]
        if not speakers:
            return {"project_id": pid, "participants": participants, "suggested": 0, "unresolved": 0}
        messages = self.store.current(pid)
        progress("models")
        self.client.models()
        suggestions: list[dict] = []
        batches = [speakers[i:i + 8] for i in range(0, len(speakers), 8)]
        for batch_no, batch in enumerate(batches, 1):
            progress(f"participants:{batch_no}:{len(batches)}")
            prompt = ("PROJECT=" + canonical(self.store.require_project(pid)["name"]) +
                      "\nSPEAKERS=" + canonical(batch) +
                      "\nCONTEXT=" + canonical(self._participant_examples(messages, batch)))
            data = self.client.chat(SYSTEM_PARTICIPANT_ROLES, prompt, participant_role_schema(batch), max_tokens=900)
            if not isinstance(data, dict) or set(data) != {"participants"} or not isinstance(data["participants"], list):
                raise OllamaError("Participant role inference returned invalid fields.")
            rows = data["participants"]
            if len(rows) != len(batch):
                raise OllamaError("Participant role inference did not cover every speaker.")
            seen = set()
            for item in rows:
                if not isinstance(item, dict) or set(item) != {"speaker", "side", "confidence"}:
                    raise OllamaError("Malformed participant role suggestion.")
                speaker, side, confidence = item["speaker"], item["side"], item["confidence"]
                if speaker not in batch or speaker in seen or side not in PARTICIPANT_SIDES:
                    raise OllamaError("Participant role inference returned an unknown/repeated speaker or side.")
                if type(confidence) is not int or not 0 <= confidence <= 100:
                    raise OllamaError("Participant role inference returned invalid confidence.")
                seen.add(speaker)
                suggestions.append({"speaker": speaker, "side": side, "confidence": confidence})
            if seen != set(batch):
                raise OllamaError("Participant role inference did not cover every speaker.")
        participants = self.store.save_participant_suggestions(pid, suggestions)
        return {"project_id": pid, "participants": participants,
                "suggested": sum(p["source"] == "ai" for p in participants),
                "unresolved": sum(p["side"] == "unknown" for p in participants)}

