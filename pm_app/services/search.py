"""Focused PM Assistant service operations.

Kept as a mixin so ``pm_app.service.Service`` remains the stable public facade.
"""
from __future__ import annotations
from ..service_support import *  # noqa: F401,F403 - shared validated pipeline primitives

class SearchServiceMixin:
    def search(self, pid: str, query: str, source_id: str | None = None,
               progress: Callable[[str], None] = lambda _: None,
               topic_id: str | None = None) -> dict:
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 1200:
            raise ValueError("Search query must contain 1 to 1200 characters.")
        messages = self.store.current(pid)
        if not messages:
            raise ValueError("No project messages to index.")
        progress("models")
        models = self.client.models()
        fp = fingerprint(messages)
        cached, chunks = self.store.index(pid)
        if not cached or cached["fingerprint"] != fp or cached["model_digest"] != models[EMBED_MODEL]:
            chunks = make_chunks(messages)
            dimensions = None
            for start in range(0, len(chunks), 8):
                progress(f"index:{min(start + 8, len(chunks))}:{len(chunks)}")
                batch = chunks[start:start + 8]
                vectors = self.client.embed([c["text"] for c in batch])
                for c, v in zip(batch, vectors, strict=True):
                    dimensions = dimensions or len(v)
                    if len(v) != dimensions:
                        raise OllamaError("Inconsistent embedding dimensions; partial index was not saved.")
                    c["vector"] = v
            self.store.save_index(pid, fp, models[EMBED_MODEL], chunks)
        progress("search")
        q_vector = self.client.embed([
            "Instruct: Given a question about a project, retrieve relevant project messages.\nQuery: " + query
        ], unload=True)[0]
        lookup = {m["id"]: m for m in messages}
        eligible = [c for c in chunks if
                    (not source_id or all(lookup[i]["source_id"] == source_id for i in c["revision_ids"])) and
                    (not topic_id or all(lookup[i]["raw"].get("topic_id") == topic_id for i in c["revision_ids"]))]
        if any(len(c["vector"]) != len(q_vector) for c in eligible):
            raise OllamaError("Embedding dimensions changed; rebuild the index after checking model versions.")
        tokens = set(re.findall(r"\w{2,}", query.casefold(), flags=re.UNICODE))
        semantic = sorted(range(len(eligible)), key=lambda i: -sum(
            a*b for a, b in zip(eligible[i]["vector"], q_vector, strict=True)))
        lexical = sorted(range(len(eligible)), key=lambda i: -len(tokens & set(
            re.findall(r"\w{2,}", eligible[i]["text"].casefold(), flags=re.UNICODE))))
        scores: dict[int, float] = defaultdict(float)
        for rank, i in enumerate(semantic):
            scores[i] += 1 / (60 + rank)
        for rank, i in enumerate(lexical):
            if tokens & set(re.findall(r"\w{2,}", eligible[i]["text"].casefold(), flags=re.UNICODE)):
                scores[i] += 1 / (60 + rank)
        chosen = sorted(scores, key=scores.get, reverse=True)[:6]
        hits = [{"sources": [source_card(lookup[r]) for r in eligible[i]["revision_ids"]]} for i in chosen]
        return {"query": query, "project_id": pid, "hits": hits,
                "total_chunks": len(eligible), "model": EMBED_MODEL,
                "notice": "search_results_not_a_complete_or_verified_answer"}

