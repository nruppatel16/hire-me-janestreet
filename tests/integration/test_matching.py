import hashlib
import re

import numpy as np
import psycopg
import pytest

from xmatch.ai.embeddings import EmbedResult
from xmatch.ai.matching import run_embed, run_retrieve
from xmatch.ai.vectors import PgArrayStore
from xmatch.ingest.runner import IngestRun

from .test_runner import FakeSource, mk, q

pytestmark = pytest.mark.integration


class FakeEmbedder:
    """Deterministic: vector derived from the words of the text, so same words -> same vector."""
    provider, model, batch_size = "fake", "fake-embed", 2

    def __init__(self, dims, fail_on_call=None):
        self.dims, self.calls, self.fail_on_call = dims, 0, fail_on_call

    def embed(self, texts):
        self.calls += 1
        if self.fail_on_call == self.calls:
            raise RuntimeError("provider down")
        vecs = []
        for t in texts:
            v = np.zeros(self.dims, dtype=np.float32)
            for w in re.findall(r"[a-z0-9]+", t.lower()):
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % self.dims] += 1
            vecs.append((v / np.linalg.norm(v)).tolist())
        return EmbedResult(vecs, 10 * len(texts), 5)


class PolySource(FakeSource):
    platform = "polymarket"
    native_id = staticmethod(lambda raw: str(raw["id"]))

    def normalize(self, raw):
        from xmatch.config import PolymarketCfg
        from xmatch.ingest import polymarket
        return polymarket.normalize(raw, PolymarketCfg(base_url="x", markets_page_limit=1, events_page_limit=1,
                                                       requests_per_second=1, source_tz="UTC"))


def pm(i, q_, vol=50000):
    return {"id": i, "question": q_, "description": "Resolves YES if so.", "endDate": "2027-01-01T00:00:00Z",
            "outcomes": '["Yes","No"]', "volumeNum": vol, "active": True, "closed": False}


@pytest.fixture
def m2(cfg):
    cfg.embeddings.model, cfg.embeddings.dims = "fake-embed", 64
    cfg.retrieval.top_k, cfg.retrieval.similarity_floor = 2, 0.3
    IngestRun(FakeSource([[mk("FED", title="Will the Fed cut rates in December?"),
                           mk("BTC", title="Will Bitcoin hit 200k this year?"),
                           mk("TINY", vol="5.00", title="Will the Fed cut rates?")]]), cfg).run()
    IngestRun(PolySource([[pm(1, "Fed rate cut in December?"), pm(2, "Bitcoin above 200k by year end?"),
                           pm(3, "Will it rain in Paris tomorrow?")]]), cfg).run()
    return cfg


def test_embeds_only_eligible_and_is_cached(m2):
    e = FakeEmbedder(64)
    st = run_embed(m2, e, PgArrayStore())
    assert st.status == "succeeded" and st.embedded == 5 and st.api_calls == 3  # TINY is below the volume gate
    st2 = run_embed(m2, FakeEmbedder(64), PgArrayStore())
    assert st2.pending == 0 and st2.api_calls == 0                              # Gate 2: zero calls on re-run
    calls = q(m2.database_url, "SELECT count(*) n, sum(tokens_in) t FROM llm_calls WHERE kind='embed'")[0]
    assert calls == {"n": 3, "t": 50}
    assert q(m2.database_url, "SELECT count(*) n FROM markets WHERE needs_embedding AND passes_volume_gate")[0]["n"] == 0


def test_text_change_reembeds_only_that_market(m2):
    run_embed(m2, FakeEmbedder(64), PgArrayStore())
    IngestRun(FakeSource([[mk("FED", title="Will the Fed cut rates in January?"),
                           mk("BTC", title="Will Bitcoin hit 200k this year?"), mk("TINY", vol="5.00")]]), m2).run()
    st = run_embed(m2, FakeEmbedder(64), PgArrayStore())
    assert st.pending == 1 and st.embedded == 1


def test_model_change_is_config_only(m2):
    run_embed(m2, FakeEmbedder(64), PgArrayStore())
    m2.embeddings.dims = 32
    st = run_embed(m2, FakeEmbedder(32), PgArrayStore())
    assert st.embedded == 5   # new key -> everything re-embedded, old vectors kept


def test_provider_failure_marks_run_failed_and_keeps_finished_batches(m2):
    st = run_embed(m2, FakeEmbedder(64, fail_on_call=2), PgArrayStore())
    assert st.status == "failed" and st.embedded == 2
    assert q(m2.database_url, "SELECT count(*) n FROM embeddings")[0]["n"] == 2
    assert q(m2.database_url, "SELECT status FROM llm_calls ORDER BY id")[-1]["status"] == "error"
    assert run_embed(m2, FakeEmbedder(64), PgArrayStore()).embedded == 3       # resumes, no duplicates


def test_retrieve_finds_the_obvious_pairs(m2):
    run_embed(m2, FakeEmbedder(64), PgArrayStore())
    st = run_retrieve(m2, PgArrayStore())
    assert st.status == "succeeded" and st.kalshi_vectors == 2 and st.polymarket_vectors == 3
    rows = q(m2.database_url, """SELECT k.native_id k, p.native_id p, c.rank_k2p FROM candidate_pairs c
                                 JOIN markets k ON k.id=c.kalshi_id JOIN markets p ON p.id=c.polymarket_id
                                 WHERE c.rank_k2p = 1 ORDER BY 1""")
    assert [(r["k"], r["p"]) for r in rows] == [("BTC", "2"), ("FED", "1")]
    # rerun replaces the set, no duplicates
    run_retrieve(m2, PgArrayStore())
    assert q(m2.database_url, "SELECT count(*) - count(DISTINCT (kalshi_id, polymarket_id)) d FROM candidate_pairs")[0]["d"] == 0
