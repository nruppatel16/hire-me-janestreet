"""Vector storage and exact cosine top-K search (D-015).

`VectorStore` is the only thing callers touch. The current implementation keeps vectors as float4[] in Postgres
and searches with numpy. At ~14k vectors that is milliseconds. Swapping to pgvector or Pinecone means writing
another VectorStore.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np


@dataclass(frozen=True)
class EmbeddingKey:
    model: str
    dims: int
    text_version: int


@dataclass
class EmbeddingRow:
    market_id: int
    content_hash: str
    input_text: str
    vector: list[float]


class VectorStore(Protocol):
    def upsert(self, conn, key: EmbeddingKey, rows: list[EmbeddingRow], run_id: int | None) -> None: ...
    def load(self, conn, key: EmbeddingKey, platform: str) -> tuple[np.ndarray, np.ndarray]: ...


# Markets that should have a current embedding: the live, gated, complete set (funnel stage 1 survivors).
ELIGIBLE_SQL = "m.listed AND m.status = 'open' AND m.passes_volume_gate AND m.matchable"


class PgArrayStore:
    def upsert(self, conn, key: EmbeddingKey, rows: list[EmbeddingRow], run_id: int | None) -> None:
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO embeddings (market_id, model, dims, text_version, content_hash, input_text, vector, run_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (market_id, model, dims, text_version) DO UPDATE SET
                     content_hash = EXCLUDED.content_hash, input_text = EXCLUDED.input_text,
                     vector = EXCLUDED.vector, embedded_at = now(), run_id = EXCLUDED.run_id""",
                [(r.market_id, key.model, key.dims, key.text_version, r.content_hash, r.input_text, r.vector, run_id)
                 for r in rows])

    def load(self, conn, key: EmbeddingKey, platform: str) -> tuple[np.ndarray, np.ndarray]:
        """Current vectors (hash matches the market) for eligible markets on one platform."""
        rows = conn.execute(
            f"""SELECT e.market_id, e.vector FROM embeddings e JOIN markets m ON m.id = e.market_id
                WHERE m.platform = %s AND {ELIGIBLE_SQL} AND e.content_hash = m.content_hash
                  AND e.model = %s AND e.dims = %s AND e.text_version = %s
                ORDER BY e.market_id""",
            (platform, key.model, key.dims, key.text_version)).fetchall()
        if not rows:
            return np.zeros(0, dtype=np.int64), np.zeros((0, key.dims), dtype=np.float32)
        ids = np.array([r["market_id"] for r in rows], dtype=np.int64)
        mat = np.array([r["vector"] for r in rows], dtype=np.float32)
        return ids, normalize_rows(mat)


def normalize_rows(mat: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


def _topk_rows(a: np.ndarray, b: np.ndarray, k: int, block_rows: int) -> tuple[np.ndarray, np.ndarray]:
    """For each row of `a`, indices into `b` of the k most similar rows, best first, and their cosine scores."""
    k = min(k, b.shape[0])
    idx = np.zeros((a.shape[0], k), dtype=np.int64)
    sims = np.zeros((a.shape[0], k), dtype=np.float32)
    if k == 0:
        return idx, sims
    for start in range(0, a.shape[0], block_rows):
        s = a[start:start + block_rows] @ b.T
        part = np.argpartition(-s, k - 1, axis=1)[:, :k]
        part_s = np.take_along_axis(s, part, axis=1)
        order = np.argsort(-part_s, axis=1, kind="stable")
        idx[start:start + block_rows] = np.take_along_axis(part, order, axis=1)
        sims[start:start + block_rows] = np.take_along_axis(part_s, order, axis=1)
    return idx, sims


@dataclass
class Candidate:
    kalshi_id: int
    polymarket_id: int
    similarity: float
    rank_k2p: int | None   # 1-based rank of the Polymarket market in the Kalshi market's top-K
    rank_p2k: int | None


def mutual_top_k(k_ids: np.ndarray, k_mat: np.ndarray, p_ids: np.ndarray, p_mat: np.ndarray,
                 k: int, floor: float, block_rows: int = 2048) -> list[Candidate]:
    """Top-K in both directions, union, then the similarity floor (PRD section 6, stage 2)."""
    found: dict[tuple[int, int], Candidate] = {}
    k2p_idx, k2p_sim = _topk_rows(k_mat, p_mat, k, block_rows)
    for i, kid in enumerate(k_ids):
        for r, (j, sim) in enumerate(zip(k2p_idx[i], k2p_sim[i], strict=True), start=1):
            if sim >= floor:
                found[(int(kid), int(p_ids[j]))] = Candidate(int(kid), int(p_ids[j]), float(sim), r, None)
    p2k_idx, p2k_sim = _topk_rows(p_mat, k_mat, k, block_rows)
    for j, pid in enumerate(p_ids):
        for r, (i, sim) in enumerate(zip(p2k_idx[j], p2k_sim[j], strict=True), start=1):
            if sim >= floor:
                key = (int(k_ids[i]), int(pid))
                if key in found:
                    found[key].rank_p2k = r
                else:
                    found[key] = Candidate(key[0], key[1], float(sim), None, r)
    return sorted(found.values(), key=lambda c: (-c.similarity, c.kalshi_id, c.polymarket_id))
