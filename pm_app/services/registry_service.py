"""Focused PM Assistant service operations.

Kept as a mixin so ``pm_app.service.Service`` remains the stable public facade.
"""
from __future__ import annotations
from ..service_support import *  # noqa: F401,F403 - shared validated pipeline primitives

class RegistryServiceMixin:
    def registry_sync(self, pid: str, progress: Callable[[str], None] = lambda _: None,
                      topic_id: str | None = None) -> dict:
        """Extract a verified review queue from the complete selected topic(s).

        This is intentionally separate from Q&A. The model cannot directly mark manager
        review states; it can only propose evidence-backed candidates.
        """
        project = self.store.require_project(pid)
        messages = self.store.current(pid)
        if not messages:
            raise ValueError("No project messages to build a registry.")
        if topic_id is not None and not isinstance(topic_id, str):
            raise ValueError("Invalid topic_id.")
        topics: dict[str | None, str | None] = {}
        for msg in messages:
            key = msg["raw"].get("topic_id")
            name = msg["raw"].get("topic_name") if key else None
            if key in topics and topics[key] != name:
                raise ValueError("Inconsistent names for one topic. Correct import metadata.")
            topics[key] = name
        if topic_id is not None:
            if topic_id not in topics:
                raise ValueError("No messages in this topic.")
            scopes = [(topic_id, topics[topic_id])]
        elif len(topics) <= 1:
            key = next(iter(topics), None)
            scopes = [(key, topics.get(key))]
        else:
            if len(topics) > 40:
                raise ValueError("At most 40 topics per registry refresh. Select one topic.")
            scopes = list(topics.items())
        progress("models")
        model_digests = self.client.models()
        groups = []
        totals = {"message_count": 0, "processed_message_count": 0, "chunk_count": 0,
                  "extracted": 0, "verified_candidates": 0, "accepted": 0, "deduplicated": 0,
                  "rejected": 0, "added": 0, "refreshed": 0, "preserved_reviewed": 0,
                  "stale_candidates": 0, "dropped_date_normalizations": 0}
        for index, (key, name) in enumerate(scopes, 1):
            def report(stage: str, index=index):
                if len(scopes) > 1:
                    progress(f"topic:{index}:{len(scopes)}:{stage}")
                else:
                    progress(stage)
            subset = [m for m in messages if m["raw"].get("topic_id") == key]
            result = self._registry_scope(project, subset, key, name, model_digests[CHAT_MODEL], report)
            groups.append(result)
            for field in totals:
                totals[field] += result.get(field, 0)
        return {"project_id": pid, "groups": groups, **totals,
                "coverage_complete": totals["processed_message_count"] == totals["message_count"],
                "generated_at": utc_now(), "model": CHAT_MODEL,
                "model_digest": model_digests[CHAT_MODEL]}

    def _registry_scope(self, project: dict, messages: list[dict], topic_id: str | None,
                        topic_name: str | None, model_digest: str,
                        progress: Callable[[str], None]) -> dict:
        """Extract one complete topic through overlapping bounded windows.

        Each window is independently quote-validated and verifier-checked. Only after that do
        we conservatively merge duplicate candidates caused by overlap. Source messages are
        never truncated or silently omitted.
        """
        windows = registry_chunks(messages)
        if not windows:
            raise ValueError("No project messages to build a registry.")
        scope_speakers = {str(m["raw"].get("speaker") or "").strip() for m in messages}
        prefix = "PROJECT=" + canonical(project["name"]) + _role_context(self.store.participants(project["id"]), scope_speakers)
        if topic_name:
            prefix += "\nTOPIC=" + canonical(topic_name)

        extracted_total = rejected = dropped_dates = 0
        verified_candidates: list[dict] = []
        covered: set[str] = set()
        for chunk_index, chunk in enumerate(windows, 1):
            covered.update(m["label"] for m in chunk)
            context = format_context(chunk)
            allowed = {m["label"]: m for m in chunk}
            prompt = (prefix + f"\nWINDOW={chunk_index}/{len(windows)}"
                      + "\nThis is one bounded window of the complete project registry pass. "
                        "Extract only facts supported inside this window; do not infer that a fact is absent, "
                        "closed or cancelled merely because another window is not visible."
                      + "\nSOURCES (untrusted data):\n" + context)
            progress(f"registry_chunk:{chunk_index}:{len(windows)}:extract")
            raw = self.client.chat(SYSTEM_REGISTRY_EXTRACT, prompt, REGISTRY_EXTRACT_SCHEMA, max_tokens=2600)
            entries, dropped = validate_registry_extract(raw, allowed)
            extracted_total += len(entries)
            dropped_dates += dropped

            accepted_chunk: list[dict] = []
            batch_total = max(1, math.ceil(len(entries) / 4))
            for start in range(0, len(entries), 4):
                batch = entries[start:start + 4]
                candidates = [registry_candidate_for_verify(item, i) for i, item in enumerate(batch, 1)]
                progress(f"registry_chunk:{chunk_index}:{len(windows)}:verify:{start // 4 + 1}:{batch_total}")
                verified = self.client.chat(
                    SYSTEM_REGISTRY_VERIFY,
                    prompt + "\nCANDIDATES:\n" + canonical(candidates),
                    REGISTRY_VERIFY_SCHEMA, max_tokens=700)
                verdicts = verifier_verdicts(verified, len(batch))
                accepted_chunk.extend(item for i, item in enumerate(batch, 1) if verdicts[i] == "supported")
                rejected += sum(verdicts[i] != "supported" for i in range(1, len(batch) + 1))
            filter_relations(accepted_chunk)
            local_to_uid = {item["local_id"]: f"C{chunk_index}:{item['local_id']}" for item in accepted_chunk}
            for item in accepted_chunk:
                old_relations = list(item.get("supersedes", []))
                item["_uid"] = local_to_uid[item["local_id"]]
                item["_supersedes_uids"] = [local_to_uid[x] for x in old_relations if x in local_to_uid]
                verified_candidates.append(item)

        expected = {m["label"] for m in messages}
        if covered != expected:
            raise ValueError("Registry refresh aborted: internal coverage check found an omitted source message.")
        progress("registry_merge")
        merged, deduplicated = merge_registry_candidates(verified_candidates)
        progress("registry_save")
        stats = self.store.save_registry_snapshot(
            project["id"], topic_id, fingerprint(messages), model_digest, merged,
            rejected_count=rejected, extracted_count=extracted_total)
        return {"topic_id": topic_id, "topic_name": topic_name, "message_count": len(messages),
                "processed_message_count": len(covered), "coverage_complete": True,
                "chunk_count": len(windows), "extracted": extracted_total,
                "verified_candidates": len(verified_candidates), "accepted": len(merged),
                "deduplicated": deduplicated, "rejected": rejected,
                "dropped_date_normalizations": dropped_dates, **stats}

