"""Slow-cadence jobs for Milestone 2: embed new/changed eligible markets, then retrieve candidate pairs."""

from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass, field
from decimal import Decimal

from psycopg.types.json import Jsonb

from xmatch.ai.embeddings import Embedder
from xmatch.ai.vectors import ELIGIBLE_SQL, EmbeddingKey, EmbeddingRow, VectorStore, mutual_top_k
from xmatch.config import Settings
from xmatch.db import connect
from xmatch.models import normalize_ws

log = logging.getLogger(__name__)


def embed_text(title: str | None, description: str | None, text_version: int) -> str:
    """Embedded text recipe. v1: title plus the short description (PRD section 6), not the full rules.
    Rules are long, boilerplate-heavy and go to the classifier instead."""
    if text_version != 1:
        raise ValueError(f"unknown text_version {text_version}")
    t, d = normalize_ws(title), normalize_ws(description)
    return f"{t}\n{d}" if d and d.lower() not in t.lower() else t


def embedding_key(s: Settings) -> EmbeddingKey:
    return EmbeddingKey(s.embeddings.model, s.embeddings.dims, s.embeddings.text_version)


def retrieval_version(s: Settings) -> str:
    e, r = s.embeddings, s.retrieval
    return f"{e.model}:{e.dims}:t{e.text_version}:k{r.top_k}:f{r.similarity_floor:.3f}"


def _start(log_conn, job: str, snapshot: dict) -> int:
    return log_conn.execute(
        "INSERT INTO ingest_runs (job, platform, config_snapshot) VALUES (%s, 'all', %s) RETURNING run_id",
        (job, Jsonb(snapshot))).fetchone()["run_id"]


def _finish(log_conn, run_id: int, status: str, read: int, written: int, errors: int) -> None:
    log_conn.execute(
        """UPDATE ingest_runs SET ended_at = now(), status = %s, rows_read = %s, rows_written = %s, error_count = %s
           WHERE run_id = %s""", (status, read, written, errors, run_id))


def _log_error(log_conn, run_id: int, stage: str, e: BaseException) -> None:
    log_conn.execute(
        "INSERT INTO ingest_errors (run_id, platform, stage, message, detail) VALUES (%s, 'all', %s, %s, %s)",
        (run_id, stage, f"{type(e).__name__}: {e}"[:2000], Jsonb({"traceback": traceback.format_exc()[-4000:]})))


@dataclass
class EmbedStats:
    run_id: int = 0
    status: str = "running"
    pending: int = 0
    embedded: int = 0
    api_calls: int = 0
    tokens_in: int = 0
    cost_usd: float = 0.0
    errors: int = 0


def pending_markets(conn, key: EmbeddingKey) -> list[dict]:
    """Eligible markets with no current embedding under this model/dims/text recipe."""
    return conn.execute(
        f"""SELECT m.id, m.title, m.description, m.content_hash FROM markets m
            LEFT JOIN embeddings e ON e.market_id = m.id AND e.model = %s AND e.dims = %s AND e.text_version = %s
            WHERE {ELIGIBLE_SQL} AND (e.market_id IS NULL OR e.content_hash <> m.content_hash)
            ORDER BY m.id""", (key.model, key.dims, key.text_version)).fetchall()


def run_embed(s: Settings, embedder: Embedder, store: VectorStore) -> EmbedStats:
    key = embedding_key(s)
    st = EmbedStats()
    price = Decimal(str(s.embeddings.price_per_mtok_usd))
    with connect(s.database_url, autocommit=True) as log_conn, connect(s.database_url) as conn:
        st.run_id = _start(log_conn, "embed", {"embeddings": s.embeddings.model_dump()})
        try:
            todo = pending_markets(conn, key)
            conn.commit()
            st.pending = len(todo)
            for i in range(0, len(todo), embedder.batch_size):
                batch = todo[i:i + embedder.batch_size]
                texts = [embed_text(m["title"], m["description"], key.text_version) for m in batch]
                try:
                    res = embedder.embed(texts)
                except Exception as e:
                    log_conn.execute(
                        """INSERT INTO llm_calls (run_id, kind, provider, model, items, latency_ms, status, error)
                           VALUES (%s, 'embed', %s, %s, %s, 0, 'error', %s)""",
                        (st.run_id, embedder.provider, embedder.model, len(texts), str(e)[:2000]))
                    raise
                st.api_calls += 1
                cost = (Decimal(res.tokens_in or 0) * price / Decimal(1_000_000)) if res.tokens_in is not None else None
                st.tokens_in += res.tokens_in or 0
                st.cost_usd += float(cost or 0)
                with conn.transaction():
                    store.upsert(conn, key, [
                        EmbeddingRow(m["id"], m["content_hash"], t, v)
                        for m, t, v in zip(batch, texts, res.vectors, strict=True)], st.run_id)
                    conn.execute("UPDATE markets SET needs_embedding = false WHERE id = ANY(%s)",
                                 ([m["id"] for m in batch],))
                log_conn.execute(
                    """INSERT INTO llm_calls (run_id, kind, provider, model, items, tokens_in, latency_ms,
                                              cost_usd, price_version, status)
                       VALUES (%s, 'embed', %s, %s, %s, %s, %s, %s, %s, 'ok')""",
                    (st.run_id, embedder.provider, embedder.model, len(texts), res.tokens_in, res.latency_ms,
                     cost, s.embeddings.price_version))
                st.embedded += len(batch)
                if st.api_calls % 20 == 0:
                    log.info("embed run %s: %d / %d", st.run_id, st.embedded, st.pending)
        except BaseException as e:
            conn.rollback()
            st.errors += 1
            _log_error(log_conn, st.run_id, "embed", e)
            _finish(log_conn, st.run_id, "failed", st.pending, st.embedded, st.errors)
            st.status = "failed"
            if isinstance(e, KeyboardInterrupt):
                raise
            return st
        _finish(log_conn, st.run_id, "succeeded", st.pending, st.embedded, 0)
        st.status = "succeeded"
    return st


@dataclass
class RetrieveStats:
    run_id: int = 0
    status: str = "running"
    version: str = ""
    kalshi_vectors: int = 0
    polymarket_vectors: int = 0
    pairs: int = 0
    similarity_hist: dict[str, int] = field(default_factory=dict)


def run_retrieve(s: Settings, store: VectorStore) -> RetrieveStats:
    """Recompute the candidate set for the current retrieval version and replace it atomically."""
    key = embedding_key(s)
    st = RetrieveStats(version=retrieval_version(s))
    with connect(s.database_url, autocommit=True) as log_conn, connect(s.database_url) as conn:
        st.run_id = _start(log_conn, "retrieve", {"retrieval": s.retrieval.model_dump(), "version": st.version})
        try:
            k_ids, k_mat = store.load(conn, key, "kalshi")
            p_ids, p_mat = store.load(conn, key, "polymarket")
            st.kalshi_vectors, st.polymarket_vectors = len(k_ids), len(p_ids)
            cands = mutual_top_k(k_ids, k_mat, p_ids, p_mat, s.retrieval.top_k, s.retrieval.similarity_floor,
                                 s.retrieval.block_rows)
            with conn.transaction():
                conn.execute("DELETE FROM candidate_pairs WHERE retrieval_version = %s AND pair_id NOT IN "
                             "(SELECT pair_id FROM verdicts)", (st.version,))
                with conn.cursor() as cur:
                    cur.executemany(
                        """INSERT INTO candidate_pairs (kalshi_id, polymarket_id, similarity, retrieval_version,
                                                        rank_k2p, rank_p2k)
                           VALUES (%s, %s, %s, %s, %s, %s)
                           ON CONFLICT (kalshi_id, polymarket_id, retrieval_version) DO UPDATE SET
                             similarity = EXCLUDED.similarity, rank_k2p = EXCLUDED.rank_k2p,
                             rank_p2k = EXCLUDED.rank_p2k""",
                        [(c.kalshi_id, c.polymarket_id, c.similarity, st.version, c.rank_k2p, c.rank_p2k)
                         for c in cands])
            st.pairs = len(cands)
            for c in cands:
                b = f"{int(c.similarity * 20) / 20:.2f}"
                st.similarity_hist[b] = st.similarity_hist.get(b, 0) + 1
        except BaseException as e:
            conn.rollback()
            _log_error(log_conn, st.run_id, "retrieve", e)
            _finish(log_conn, st.run_id, "failed", st.kalshi_vectors + st.polymarket_vectors, 0, 1)
            st.status = "failed"
            if isinstance(e, KeyboardInterrupt):
                raise
            return st
        _finish(log_conn, st.run_id, "succeeded", st.kalshi_vectors + st.polymarket_vectors, st.pairs, 0)
        st.status = "succeeded"
    return st
