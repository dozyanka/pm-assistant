"""No cloud fallback, download, proxy, redirect, plugin or tool execution."""
from __future__ import annotations
import http.client
import json
import math
import os
from typing import Any

CHAT_MODEL = "qwen3.5:9b"
EMBED_MODEL = "qwen3-embedding:0.6b"
NUM_CTX = 8192
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
ALLOWED_PATHS = {"/api/chat", "/api/embed", "/api/tags", "/api/version", "/api/ps"}


class OllamaError(RuntimeError):
    pass


class OllamaClient:
    ALLOWED_HOSTS = {"127.0.0.1", "host.docker.internal", "ollama"}

    def __init__(self, host: str | None = None):
        # Only counters: never persist prompts, model output or thinking here.
        configured = (host or os.environ.get("PM_OLLAMA_HOST") or "127.0.0.1").strip()
        if configured not in self.ALLOWED_HOSTS:
            raise OllamaError("Blocked Ollama host. Only the local host or the bundled Docker Ollama service is permitted.")
        self.host = configured
        self.last_chat_stats: dict = {}

    def request(self, path: str, payload: dict | None = None) -> dict:
        if path not in ALLOWED_PATHS:
            raise OllamaError("Blocked API endpoint.")
        body = None if payload is None else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        connection = http.client.HTTPConnection(self.host, 11434, timeout=600)
        try:
            connection.request("GET" if payload is None else "POST", path, body=body,
                               headers={"Content-Type": "application/json; charset=utf-8"})
            response = connection.getresponse()
            if response.status != 200:
                # Never follow redirects, even back to loopback.
                raise OllamaError(f"Ollama HTTP {response.status}. Check the Ollama window and installed models.")
            data = response.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise OllamaError("Ollama response exceeded the size limit.")
            result = json.loads(data)
            if not isinstance(result, dict) or result.get("error"):
                raise OllamaError("Ollama returned an invalid response or a model error.")
            return result
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise OllamaError("Local Ollama request failed. Keep start_ollama.cmd running; see its console.") from exc
        finally:
            connection.close()

    def models(self) -> dict[str, str]:
        data = self.request("/api/tags")
        available = {}
        for item in data.get("models", []):
            name = item.get("name")
            if name in (CHAT_MODEL, EMBED_MODEL):
                if item.get("remote_host") or item.get("remote_model"):
                    raise OllamaError("Remote model configuration is not permitted.")
                available[name] = item.get("digest", "")
        for name in (CHAT_MODEL, EMBED_MODEL):
            if not available.get(name):
                raise OllamaError("Required local model is missing: " + name + ". No automatic download is performed.")
        return available

    def embed(self, texts: list[str], unload: bool = False) -> list[list[float]]:
        if not texts:
            return []
        result = self.request("/api/embed", {"model": EMBED_MODEL, "input": texts,
                              "truncate": False, "keep_alive": 0 if unload else "5m"})
        vectors = result.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise OllamaError("Unexpected number of embedding vectors.")
        return [normalize_vector(v) for v in vectors]

    def chat(self, system: str, user: str, schema: dict, max_tokens: int = 2048) -> dict:
        # Conservative application limit, not a tokenizer. Large inputs are rejected,
        # never silently shortened or replaced with a summary.
        self.last_chat_stats = {}
        # The format field constrains decoding, but is not a substitute for
        # explaining the response structure to the model in its prompt.
        grounded_system = system + "\nJSON_SCHEMA:\n" + json.dumps(
            schema, ensure_ascii=False, separators=(",", ":"))
        if len(grounded_system) + len(user) > 11500:
            raise OllamaError("The full source context is too large for this prototype. Select one source, or use source search.")
        result = self.request("/api/chat", {
            "model": CHAT_MODEL, "stream": False, "think": False, "format": schema,
            "messages": [{"role": "system", "content": grounded_system}, {"role": "user", "content": user}],
            "options": {"num_ctx": NUM_CTX, "temperature": 0, "num_predict": max_tokens},
            "keep_alive": "5m",
        })
        for source, target in (("prompt_eval_count", "prompt_tokens"),
                               ("eval_count", "output_tokens")):
            value = result.get(source)
            if type(value) is int and value >= 0:
                self.last_chat_stats[target] = value
        if result.get("done_reason") in ("stop", "length"):
            self.last_chat_stats["done_reason"] = result["done_reason"]
        if result.get("done") is False:
            raise OllamaError("Ollama returned an incomplete response. It was not accepted as an answer.")
        if result.get("done_reason") == "length":
            raise OllamaError("The model answer reached its output limit. It was not accepted as a complete answer.")
        if result.get("prompt_eval_count", 0) >= NUM_CTX - max_tokens - 256:
            raise OllamaError("The prompt was too close to the context limit. Answer rejected; select a smaller source scope.")
        try:
            data = json.loads(result["message"]["content"])
        except (KeyError, TypeError, ValueError) as exc:
            raise OllamaError("The model did not return valid structured JSON. No answer was saved.") from exc
        if not isinstance(data, dict):
            raise OllamaError("Expected a JSON object from the model.")
        return data


def normalize_vector(vector: Any) -> list[float]:
    if not isinstance(vector, list) or not 1 <= len(vector) <= 4096:
        raise OllamaError("Invalid embedding dimensions.")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in vector):
        raise OllamaError("Embedding vector contains invalid values.")
    norm = math.sqrt(sum(v*v for v in vector))
    if norm <= 0:
        raise OllamaError("Embedding vector is zero.")
    return [v / norm for v in vector]
