"""Provider-agnostic embedding interface (PRD AI-2: the model is a config value).

Callers depend on `Embedder`. Adding a provider means adding a class and a branch in `make_embedder`;
no caller changes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

from xmatch.config import Settings
from xmatch.http import FetchError, ProviderClient


@dataclass
class EmbedResult:
    vectors: list[list[float]]
    tokens_in: int | None
    latency_ms: int


class Embedder(Protocol):
    provider: str
    model: str
    dims: int
    batch_size: int

    def embed(self, texts: list[str]) -> EmbedResult: ...


class GeminiEmbedder:
    provider = "gemini"

    def __init__(self, s: Settings, client: ProviderClient | None = None):
        cfg, pcfg = s.embeddings, s.providers["gemini"]
        self.model, self.dims, self.batch_size = cfg.model, cfg.dims, cfg.batch_size
        self._client = client or ProviderClient(
            pcfg.base_url, s.http, pcfg.requests_per_second, headers={"x-goog-api-key": s.api_key("gemini")})

    def embed(self, texts: list[str]) -> EmbedResult:
        if not texts:
            return EmbedResult([], 0, 0)
        if len(texts) > self.batch_size:
            raise ValueError(f"batch of {len(texts)} exceeds provider limit {self.batch_size}")
        body = {"requests": [
            {"model": f"models/{self.model}", "content": {"parts": [{"text": t}]},
             "output_dimensionality": self.dims}
            for t in texts]}
        t0 = time.monotonic()
        resp = self._client.post_json(f"/models/{self.model}:batchEmbedContents", body)
        latency = int((time.monotonic() - t0) * 1000)
        vectors = [e["values"] for e in resp.get("embeddings", [])]
        if len(vectors) != len(texts) or any(len(v) != self.dims for v in vectors):
            raise FetchError(f"embedding response shape mismatch: {len(vectors)} vectors for {len(texts)} texts",
                             url=self.model)
        return EmbedResult(vectors, (resp.get("usageMetadata") or {}).get("promptTokenCount"), latency)


class OllamaEmbedder:
    """Local open-weights embeddings via an Ollama server (D-023). No key, no quota, no cost."""
    provider = "ollama"

    def __init__(self, s: Settings, client: ProviderClient | None = None):
        cfg, pcfg = s.embeddings, s.providers["ollama"]
        self.model, self.dims, self.batch_size = cfg.model, cfg.dims, cfg.batch_size
        self._client = client or ProviderClient(pcfg.base_url, s.http, pcfg.requests_per_second)

    def embed(self, texts: list[str]) -> EmbedResult:
        if not texts:
            return EmbedResult([], 0, 0)
        t0 = time.monotonic()
        resp = self._client.post_json("/api/embed", {"model": self.model, "input": texts, "dimensions": self.dims})
        latency = int((time.monotonic() - t0) * 1000)
        vectors = resp.get("embeddings") or []
        if len(vectors) != len(texts) or any(len(v) != self.dims for v in vectors):
            raise FetchError(f"embedding response shape mismatch: {len(vectors)} vectors for {len(texts)} texts",
                             url=self.model)
        return EmbedResult(vectors, resp.get("prompt_eval_count"), latency)


def make_embedder(s: Settings) -> Embedder:
    if s.embeddings.provider == "gemini":
        return GeminiEmbedder(s)
    if s.embeddings.provider == "ollama":
        return OllamaEmbedder(s)
    raise ValueError(f"unknown embeddings provider: {s.embeddings.provider}")
