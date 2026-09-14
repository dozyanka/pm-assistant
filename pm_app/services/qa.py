"""Focused PM Assistant service operations.

Kept as a mixin so ``pm_app.service.Service`` remains the stable public facade.
"""
from __future__ import annotations
from ..service_support import *  # noqa: F401,F403 - shared validated pipeline primitives

class QAServiceMixin:
    def _group_status(self, statuses: list[str]) -> str:
        if not statuses:
            return "not_found"
        if all(x == "not_found" for x in statuses):
            return "not_found"
        if all(x == "not_verified" for x in statuses):
            return "not_verified"
        if all(x == "evidence_only" for x in statuses):
            return "evidence_only"
        if all(x == "supported" for x in statuses):
            return "supported"
        if "conflict" in statuses:
            return "conflict"
        return "partial"

    def _scoped_messages(self, pid: str, source_id: str | None = None,
                         topic_id: str | None = None) -> list[dict]:
        messages = self.store.current(pid, source_id)
        if topic_id is not None:
            messages = [m for m in messages if m["raw"].get("topic_id") == topic_id]
        return messages

    def _ask_complete_scope(self, pid: str, question: str, source_id: str | None = None,
            progress: Callable[[str], None] = lambda _: None,
            topic_id: str | None = None, topic_name: str | None = None,
            filter_topic: bool = False, save: bool = True) -> dict:
        messages = self._scoped_messages(pid, source_id, topic_id if filter_topic else None)
        if not messages:
            raise ValueError("No messages in the selected scope. Import data or select another source.")
        if len(format_context(messages)) <= 6400:
            return self._ask_scope(pid, question, source_id, progress, topic_id, topic_name, filter_topic, save)
        if source_id is not None:
            raise ValueError("The selected source is too large for the current prototype. Narrow the scope or use source search; no messages were silently omitted.")

        grouped: dict[str, list[dict]] = defaultdict(list)
        source_types: dict[str, str] = {}
        for msg in messages:
            grouped[msg["source_id"]].append(msg)
            source_types.setdefault(msg["source_id"], msg["source_type"])
        if len(grouped) <= 1:
            raise ValueError("The selected scope is too large for the current prototype. Narrow the scope or use source search; no messages were silently omitted.")

        for subset in grouped.values():
            if len(format_context(subset)) > 6400:
                raise ValueError("One source is too large for the current prototype. Select that source or use source search; no messages were silently omitted.")

        groups = []
        failures: list[tuple[str, Exception]] = []
        ordered_source_ids = list(grouped)
        for index, sid in enumerate(ordered_source_ids, 1):
            def report(stage, index=index):
                progress(f"source:{index}:{len(ordered_source_ids)}:{stage}")
            part = None
            last_error: Exception | None = None
            for attempt in range(2):
                try:
                    if attempt:
                        report("retry")
                    part = self._ask_scope(pid, question, sid, report, topic_id, topic_name, filter_topic, save=False)
                    break
                except OllamaError as exc:
                    last_error = exc
            if part is None:
                failures.append((sid, last_error or OllamaError("Source answer failed.")))
                rows = grouped[sid]
                part = {
                    "status": "not_verified", "claims": [], "uncertainties": [],
                    "message": "Этот источник не удалось проверить.", "sources": [],
                    "evidence_candidates": [], "dropped_claims": 0, "dropped_uncertainties": 0,
                    "scope": {"project_id": pid, "source_id": sid, "topic_id": topic_id,
                              "topic_name": topic_name, "message_count": len(rows),
                              "scope_complete": False, "fingerprint": fingerprint(rows),
                              "date_warning": any(not m["raw"].get("occurred_at") for m in rows),
                              "last_import": max(m["imported_at"] for m in rows)},
                    "model": CHAT_MODEL, "model_digest": "", "verification": "failed",
                    "diagnostics": {"pipeline_version": __version__, "message_count": len(rows),
                                    "source_count": 1, "final_status": "not_verified", "steps": []},
                    "generated_at": utc_now(), "notice": "", "date_notice": ""
                }
            channel = next((str(m["raw"].get("channel") or "").strip() for m in grouped[sid]
                            if str(m["raw"].get("channel") or "").strip()), "")
            groups.append({
                "source_id": sid,
                "source_type": source_types[sid],
                "label": channel or source_types[sid] or "Источник",
                "answer": part,
            })

        if failures and len(failures) == len(groups):
            raise failures[0][1]
        statuses = [g["answer"]["status"] for g in groups]
        result = {
            "status": self._group_status(statuses), "grouping": "source", "groups": groups,
            "failed_source_count": len(failures),
            "claims": [], "uncertainties": [],
            "sources": [src for g in groups for src in g["answer"]["sources"]],
            "message": "", "evidence_candidates": [],
            "dropped_claims": sum(g["answer"]["dropped_claims"] for g in groups),
            "scope": {"project_id": pid, "source_id": None, "topic_id": topic_id,
                      "topic_name": topic_name, "message_count": len(messages), "scope_complete": not failures,
                      "fingerprint": fingerprint(messages), "last_import": max(m["imported_at"] for m in messages)},
            "model": CHAT_MODEL, "model_digest": next((g["answer"].get("model_digest") for g in groups
                                                       if g["answer"].get("model_digest")), ""),
            "verification": "separate-complete-source-scopes" if not failures else "partial-source-verification",
            "diagnostics": {"pipeline_version": __version__, "message_count": len(messages),
                "group_count": len(groups), "grouping": "source", "scope_complete": not failures,
                "failed_source_count": len(failures),
                "groups": [g["answer"]["diagnostics"] for g in groups]},
            "generated_at": utc_now(), "notice": DRAFT_NOTICE, "date_notice": ""}
        progress("save")
        if save:
            result["answer_id"] = self.store.save_answer(pid, question.strip(), result)
        return result

    def ask(self, pid: str, question: str, source_id: str | None = None,
            progress: Callable[[str], None] = lambda _: None,
            topic_id: str | None = None) -> dict:
        messages = self.store.current(pid, source_id)
        if topic_id is not None and not isinstance(topic_id, str):
            raise ValueError("Invalid topic_id.")
        topics = {}
        for msg in messages:
            key = msg["raw"].get("topic_id")
            name = msg["raw"].get("topic_name") if key else None
            if key in topics and topics[key] != name:
                raise ValueError("Inconsistent names for one topic. Correct import metadata.")
            topics[key] = name
        if topic_id is not None:
            if topic_id not in topics:
                raise ValueError("No messages in this topic/source scope.")
            return self._ask_complete_scope(pid, question, source_id, progress, topic_id, topics[topic_id], True)
        if len(topics) > 8:
            raise ValueError("At most 8 topics per answer. Select a topic or one source.")
        if len(topics) <= 1:
            key = next(iter(topics), None)
            return self._ask_complete_scope(pid, question, source_id, progress, key, topics.get(key), bool(key))
        # A test container may contain multiple explicitly labelled original projects.
        # Read ALL selected records, but never let a model merge their agreements.
        # Each topic is a full bounded QA run; no retrieval selection or truncation.
        groups = []
        for index, (key, name) in enumerate(topics.items(), 1):
            def report(stage, index=index):
                progress(f"topic:{index}:{len(topics)}:{stage}")
            part = self._ask_complete_scope(pid, question, source_id, report, key, name, True, save=False)
            groups.append({"topic_id": key, "topic_name": name, "label": name or key or "Без темы", "answer": part})
        statuses = [g["answer"]["status"] for g in groups]
        result = {
            "status": self._group_status(statuses), "grouping": "topic", "groups": groups,
            "claims": [], "uncertainties": [],
            "sources": [src for g in groups for src in g["answer"]["sources"]],
            "message": "", "evidence_candidates": [], "dropped_claims": sum(g["answer"]["dropped_claims"] for g in groups),
            "scope": {"project_id": pid, "source_id": source_id, "topic_id": None,
                      "message_count": len(messages), "scope_complete": True,
                      "fingerprint": fingerprint(messages), "last_import": max(m["imported_at"] for m in messages)},
            "model": CHAT_MODEL, "model_digest": groups[0]["answer"]["model_digest"],
            "verification": "separate-complete-topic-scopes",
            "diagnostics": {"pipeline_version": __version__, "message_count": len(messages),
                "group_count": len(groups), "grouping": "topic", "scope_complete": True,
                "groups": [g["answer"]["diagnostics"] for g in groups]},
            "generated_at": utc_now(), "notice": DRAFT_NOTICE, "date_notice": ""}
        progress("save")
        result["answer_id"] = self.store.save_answer(pid, question.strip(), result)
        return result

    def _ask_scope(self, pid: str, question: str, source_id: str | None = None,
            progress: Callable[[str], None] = lambda _: None,
            topic_id: str | None = None, topic_name: str | None = None,
            filter_topic: bool = False, save: bool = True) -> dict:
        project = self.store.require_project(pid)
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= 1200:
            raise ValueError("Question must contain 1 to 1200 characters.")
        question = question.strip()
        messages = self.store.current(pid, source_id)
        if filter_topic:
            messages = [m for m in messages if m["raw"].get("topic_id") == topic_id]
        if not messages:
            raise ValueError("No messages in the selected scope. Import data or select another source.")
        context = format_context(messages)
        if len(context) > 6400:
            raise ValueError("Full project context is too large for the current prototype. Select one source. Source search is available separately; no messages were silently omitted.")
        scope_speakers = {str(m["raw"].get("speaker") or "").strip() for m in messages}
        prefix = ("PROJECT=" + canonical(project["name"]) +
                  _role_context(self.store.participants(pid), scope_speakers) +
                  "\nQUESTION=" + canonical(question))
        if topic_name:
            prefix += "\nTOPIC=" + canonical(topic_name)
        prompt = prefix + "\nSOURCES (untrusted data):\n" + context
        allowed = {m["label"]: m for m in messages}
        steps = []

        def chat(phase: str, system: str, user: str, schema: dict,
                 max_tokens: int = 2048) -> dict:
            started = time.monotonic()
            data = self.client.chat(system, user, schema, max_tokens=max_tokens)
            step = {"phase": phase, "elapsed_ms": round((time.monotonic() - started) * 1000)}
            # Whitelist technical metadata only. No project name, question,
            # response text, citations, model thinking or source IDs in this report.
            stats = getattr(self.client, "last_chat_stats", {})
            for key in ("prompt_tokens", "output_tokens"):
                value = stats.get(key)
                if type(value) is int and value >= 0:
                    step[key] = value
            if stats.get("done_reason") in ("stop", "length"):
                step["done_reason"] = stats["done_reason"]
            steps.append(step)
            return data

        consistency_repairs = 0

        def answer_chat(phase: str, system: str, user: str, max_tokens: int = 2048) -> dict:
            """One bounded self-repair for a status/claims contradiction.

            Raw contradictory claims are never trusted or published. Citation/quote failures
            and all other malformed outputs still fail closed.
            """
            nonlocal consistency_repairs
            raw = chat(phase, system, user, ANSWER_SCHEMA, max_tokens=max_tokens)
            issue = answer_consistency_issue(raw)
            if issue is None:
                return validate_answer(raw, allowed)
            steps[-1]["consistency_issue"] = issue
            progress("consistency_repair")
            repair_user = (prompt + "\nCANDIDATE (untrusted draft):\n" + canonical(raw) +
                           "\nCONSISTENCY_ISSUE=" + canonical(issue))
            repaired_raw = chat(phase + "_consistency_repair", SYSTEM_CONSISTENCY_REPAIR,
                                repair_user, ANSWER_SCHEMA, max_tokens=min(max_tokens, 1400))
            consistency_repairs += 1
            remaining = answer_consistency_issue(repaired_raw)
            steps[-1]["consistency_issue_after_repair"] = remaining
            if remaining is not None:
                raise OllamaError(
                    "Локальная модель повторно вернула внутренне противоречивый черновик. "
                    "Ответ не опубликован; повторите запрос.")
            return validate_answer(repaired_raw, allowed)

        progress("models")
        model_digests = self.client.models()
        progress("draft")
        draft = answer_chat("draft", SYSTEM_QA, prompt)
        initial_status = draft["status"]
        steps[-1].update(status=initial_status, claim_count=len(draft["claims"]))
        selected_ids: set[str] = set()
        recovery_used = initial_status == "not_found"
        window_count = 0
        if recovery_used:
            # A valid empty JSON object is not evidence that the sources are empty.
            # One bounded recovery cycle: locate originals, then redraft once.
            windows = recall_windows(messages)
            window_count = len(windows)
            for number, window in enumerate(windows, 1):
                progress(f"recall:{number}:{len(windows)}")
                labels = [m["label"] for m in window]
                located = validate_locations(chat(
                    f"locate_{number}", SYSTEM_LOCATE,
                    prefix + "\nSOURCES (untrusted data):\n" + format_context(window),
                    locate_schema(labels), max_tokens=384), set(labels))
                selected_ids.update(located)
                steps[-1].update(message_count=len(window), selected_count=len(located))
            if selected_ids:
                progress("retry")
                # Retain ALL original context. Selection must not hide a later
                # cancellation, a competing version, or an important exception.
                hints = [m["label"] for m in messages if m["label"] in selected_ids]
                retry_prompt = prompt + "\nREVIEW_IDS (retrieval hints only):\n" + canonical(hints)
                draft = answer_chat("retry", SYSTEM_RECOVER, retry_prompt)
                steps[-1].update(status=draft["status"], claim_count=len(draft["claims"]))

        # One bounded repair for objectively risky wording; never rewrite facts
        # using string replacement. IDs/quotes are revalidated after the repair.
        issues = wording_issues(draft["claims"], allowed)
        wording_repair_used = bool(issues)
        policy_dropped = 0
        original_guard_ids = set()
        if issues:
            original_guard_ids = {e["id"] for c in draft["claims"] for e in c["evidence"]}
            progress("repair")
            repaired = answer_chat("wording_repair", SYSTEM_REPAIR,
                prompt + "\nCANDIDATE:\n" + canonical(draft) + "\nISSUES:\n" + canonical(issues))
            remaining = wording_issues(repaired["claims"], allowed)
            bad_numbers = {i["claim_number"] for i in remaining}
            policy_dropped = len(bad_numbers)
            repaired["claims"] = [c for i, c in enumerate(repaired["claims"], 1) if i not in bad_numbers]
            if not repaired["claims"]:
                policy_dropped = max(1, policy_dropped)
            draft = repaired
            steps[-1].update(guard_issue_count=len(issues), remaining_issue_count=len(remaining))
        dropped = policy_dropped
        verification_ran = bool(draft["claims"])
        if draft["claims"]:
            progress("verify")
            checked = []
            verifier_modality_rechecks = 0
            for start in range(0, len(draft["claims"]), 3):
                batch = draft["claims"][start:start + 3]
                kinds = [infer_claim_kind(c, allowed) for c in batch]
                candidates = [{"claim_number": i + 1, "kind": kinds[i], **c}
                              for i, c in enumerate(batch)]
                verify_user = prompt + "\nCANDIDATES:\n" + canonical(candidates)
                verdicts = verifier_verdicts(
                    chat(f"verify_{start // 3 + 1}", SYSTEM_VERIFY,
                         verify_user, VERIFY_SCHEMA, max_tokens=500), len(batch))
                initial_verdicts = dict(verdicts)
                verify_step = steps[-1]
                verify_step["claim_kinds"] = kinds
                verify_step["verdicts"] = [initial_verdicts[i] for i in range(1, len(batch) + 1)]

                # A same-model verifier can confuse "the event will happen" with
                # "the speaker promised the event". Recheck only speech-act /
                # agreement kinds, never generic facts or completion claims. The
                # second pass can rescue a claim only by explicitly returning supported.
                retry_positions = [i for i in range(1, len(batch) + 1)
                                   if verdicts[i] != "supported" and verifier_recheck_allowed(kinds[i - 1])]
                recheck_verdicts = {}
                if retry_positions:
                    progress("verify_modality")
                    retry_candidates = [
                        {"claim_number": local_i, "kind": kinds[pos - 1], **batch[pos - 1]}
                        for local_i, pos in enumerate(retry_positions, 1)
                    ]
                    retry_user = prompt + "\nCANDIDATES:\n" + canonical(retry_candidates)
                    local = verifier_verdicts(chat(
                        f"verify_modality_{start // 3 + 1}", SYSTEM_VERIFY_MODALITY_RECHECK,
                        retry_user, VERIFY_SCHEMA, max_tokens=500), len(retry_candidates))
                    verifier_modality_rechecks += 1
                    for local_i, original_pos in enumerate(retry_positions, 1):
                        recheck_verdicts[original_pos] = local[local_i]
                        if local[local_i] == "supported":
                            verdicts[original_pos] = "supported"

                verify_step["final_verdicts"] = [verdicts[i] for i in range(1, len(batch) + 1)]
                if recheck_verdicts:
                    recheck_step = steps[-1]
                    recheck_step["claim_kinds"] = [kinds[i - 1] for i in retry_positions]
                    recheck_step["verdicts"] = [recheck_verdicts[i] for i in retry_positions]
                checked.extend(c for i, c in enumerate(batch, 1) if verdicts[i] == "supported")
                dropped += sum(v != "supported" for v in verdicts.values())
            draft["claims"] = checked
        else:
            verifier_modality_rechecks = 0

        # Unknowns are also generated assertions: validate them, not just claims.
        dropped_uncertainties = 0
        if draft["claims"] and draft["uncertainties"]:
            checked_unknowns = []
            for start in range(0, len(draft["uncertainties"]), 3):
                batch = draft["uncertainties"][start:start + 3]
                candidates = [{"claim_number": i, "kind": "uncertainty", "text": text, "evidence": []}
                              for i, text in enumerate(batch, 1)]
                progress("verify_unknowns")
                verdicts = verifier_verdicts(chat(f"verify_unknowns_{start // 3 + 1}", SYSTEM_VERIFY,
                    prompt + "\nCANDIDATES:\n" + canonical(candidates), VERIFY_SCHEMA, max_tokens=500), len(batch))
                steps[-1]["verdicts"] = [verdicts[i] for i in range(1, len(batch) + 1)]
                checked_unknowns.extend(text for i, text in enumerate(batch, 1) if verdicts[i] == "supported")
                dropped_uncertainties += sum(v != "supported" for v in verdicts.values())
            draft["uncertainties"] = checked_unknowns
        selected_ids.update(original_guard_ids)
        used_ids = {ev["id"] for c in draft["claims"] for ev in c["evidence"]}
        status = draft["status"]
        if dropped:
            status = "partial" if draft["claims"] else "not_verified"
        elif status == "not_found" and selected_ids:
            status = "evidence_only"
        if not draft["claims"]:
            draft["uncertainties"] = []

        result = {
            "status": status, "claims": draft["claims"], "uncertainties": draft["uncertainties"],
            "message": (NOT_VERIFIED if status == "not_verified" else
                        EVIDENCE_ONLY if status == "evidence_only" else
                        NOT_FOUND if status == "not_found" else ""),
            "sources": [source_card(m) for m in messages if m["label"] in used_ids],
            "evidence_candidates": candidate_context(messages, selected_ids)
                if status in ("evidence_only", "not_verified") and selected_ids else [],
            "scope": {"project_id": pid, "source_id": source_id, "topic_id": topic_id,
                      "topic_name": topic_name, "message_count": len(messages),
                      "scope_complete": True, "fingerprint": fingerprint(messages),
                      "date_warning": any(not m["raw"].get("occurred_at") for m in messages),
                      "last_import": max(m["imported_at"] for m in messages)},
            "model": CHAT_MODEL, "model_digest": model_digests[CHAT_MODEL],
            "verification": "same-model-second-pass" if verification_ran else "not-run",
            "dropped_claims": dropped, "dropped_uncertainties": dropped_uncertainties,
            "diagnostics": {
                "pipeline_version": __version__, "model": CHAT_MODEL,
                "model_digest": model_digests[CHAT_MODEL],
                "message_count": len(messages),
                "source_count": len({m["source_id"] for m in messages}),
                "context_characters": len(context), "initial_status": initial_status,
                "recovery_used": recovery_used, "recovery_windows": window_count,
                "selected_message_count": len(selected_ids),
                "verification_ran": verification_ran, "final_status": status,
                "final_claim_count": len(draft["claims"]), "dropped_claims": dropped,
                "wording_repair_used": wording_repair_used, "policy_dropped_claims": policy_dropped,
                "consistency_repairs": consistency_repairs,
                "verifier_modality_rechecks": verifier_modality_rechecks,
                "dropped_uncertainties": dropped_uncertainties, "steps": steps
            },
            "generated_at": utc_now(), "notice": DRAFT_NOTICE,
            "date_notice": UNKNOWN_DATES if any(not m["raw"].get("occurred_at") for m in messages) else ""
        }
        progress("save")
        if save:
            result["answer_id"] = self.store.save_answer(pid, question, result)
        return result

