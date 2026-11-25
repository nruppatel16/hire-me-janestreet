import numpy as np

from xmatch.ai.matching import embed_text
from xmatch.ai.vectors import mutual_top_k, normalize_rows


def brute(k_mat, p_mat, k, floor):
    s = k_mat @ p_mat.T
    out = set()
    for i in range(s.shape[0]):
        for j in np.argsort(-s[i])[:k]:
            if s[i, j] >= floor:
                out.add((i, int(j)))
    for j in range(s.shape[1]):
        for i in np.argsort(-s[:, j])[:k]:
            if s[i, j] >= floor:
                out.add((int(i), j))
    return out


def test_mutual_top_k_matches_brute_force_union():
    rng = np.random.default_rng(0)
    k_mat = normalize_rows(rng.normal(size=(37, 16)).astype(np.float32))
    p_mat = normalize_rows(rng.normal(size=(23, 16)).astype(np.float32))
    k_ids, p_ids = np.arange(100, 137), np.arange(500, 523)
    got = mutual_top_k(k_ids, k_mat, p_ids, p_mat, k=3, floor=0.1, block_rows=5)  # small blocks exercise chunking
    want = {(int(k_ids[i]), int(p_ids[j])) for i, j in brute(k_mat, p_mat, 3, 0.1)}
    assert {(c.kalshi_id, c.polymarket_id) for c in got} == want
    assert all(c.rank_k2p or c.rank_p2k for c in got)
    assert [c.similarity for c in got] == sorted((c.similarity for c in got), reverse=True)


def test_identical_vectors_rank_first_both_ways():
    v = normalize_rows(np.eye(4, dtype=np.float32) + 0.01)
    got = mutual_top_k(np.array([1, 2]), v[:2], np.array([7, 8]), v[:2].copy(), k=1, floor=0.5)
    assert {(c.kalshi_id, c.polymarket_id, c.rank_k2p, c.rank_p2k) for c in got} == {(1, 7, 1, 1), (2, 8, 1, 1)}


def test_floor_and_empty_inputs():
    v = normalize_rows(np.eye(3, dtype=np.float32))
    assert mutual_top_k(np.arange(3), v, np.arange(3), v, k=3, floor=0.99) != []
    assert mutual_top_k(np.arange(3), v, np.zeros(0, dtype=np.int64), np.zeros((0, 3), np.float32), 3, 0) == []


def test_embed_text_recipe():
    assert embed_text("Will X win?", "Nov 28 · X wins", 1) == "Will X win?\nNov 28 · X wins"
    assert embed_text("Will X win the cup?", "x win", 1) == "Will X win the cup?"  # no duplicate
    assert embed_text("  Will   X? ", None, 1) == "Will X?"
