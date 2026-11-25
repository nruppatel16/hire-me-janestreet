"""Recall vs candidate-count curve for choosing the similarity floor (PRD section 6: "not picked by feel").

Uses only the dev split: the frozen holdout is never used for tuning (PRD section 7).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from xmatch.ai.matching import embedding_key
from xmatch.ai.vectors import PgArrayStore, mutual_top_k
from xmatch.config import Settings


@dataclass
class CurvePoint:
    floor: float
    recall: float
    found: int
    must_confirm: int
    candidates: int


@dataclass
class CurveResult:
    points: list[CurvePoint]
    missing: list[str]          # labeled pairs that have no current embedding (not eligible / text changed)
    per_pair: list[dict]


def recall_curve(conn, s: Settings, floors: list[float] | None = None, k: int | None = None) -> CurveResult:
    k = k or s.retrieval.top_k
    floors = floors or [round(x, 3) for x in np.arange(0.50, 0.951, 0.025)]
    store = PgArrayStore()
    key = embedding_key(s)
    k_ids, k_mat = store.load(conn, key, "kalshi")
    p_ids, p_mat = store.load(conn, key, "polymarket")
    k_pos = {int(x): i for i, x in enumerate(k_ids)}
    p_pos = {int(x): i for i, x in enumerate(p_ids)}

    cases = conn.execute(
        """SELECT DISTINCT ON (e.pair_ref) e.pair_ref, k.id AS kid, p.id AS pid
           FROM eval_cases e
           LEFT JOIN markets k ON k.platform = 'kalshi' AND k.native_id = e.kalshi_native_id
           LEFT JOIN markets p ON p.platform = 'polymarket' AND p.native_id = e.polymarket_native_id
           WHERE e.split = 'dev' AND e.label = 'must_confirm'
             AND NOT EXISTS (SELECT 1 FROM eval_cases newer WHERE newer.supersedes = e.id)
           ORDER BY e.pair_ref, e.id DESC""").fetchall()

    per_pair, missing = [], []
    for c in cases:
        if c["kid"] not in k_pos or c["pid"] not in p_pos:
            missing.append(c["pair_ref"])
            continue
        i, j = k_pos[c["kid"]], p_pos[c["pid"]]
        sim = float(k_mat[i] @ p_mat[j])
        rank_k2p = int((k_mat[i] @ p_mat.T > sim).sum()) + 1   # how many Polymarket markets beat the true match
        rank_p2k = int((p_mat[j] @ k_mat.T > sim).sum()) + 1
        per_pair.append({"pair_ref": c["pair_ref"], "similarity": sim, "rank_k2p": rank_k2p, "rank_p2k": rank_p2k,
                         "within_k": min(rank_k2p, rank_p2k) <= k})

    all_cands = mutual_top_k(k_ids, k_mat, p_ids, p_mat, k, floor=-1.0, block_rows=s.retrieval.block_rows)
    sims = np.array(sorted((c.similarity for c in all_cands)), dtype=np.float32)
    n = len(per_pair) + len(missing)
    points = []
    for f in floors:
        found = sum(1 for p in per_pair if p["within_k"] and p["similarity"] >= f)
        cands = int(len(sims) - np.searchsorted(sims, f, side="left"))
        points.append(CurvePoint(f, found / n if n else 0.0, found, n, cands))
    return CurveResult(points, missing, per_pair)
