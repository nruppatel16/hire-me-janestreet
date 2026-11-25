import psycopg
import pytest
from psycopg.rows import dict_row

from xmatch.ai.labeling import choose_split, label_session, next_pairs
from xmatch.ai.matching import run_embed, run_retrieve
from xmatch.ai.recall import recall_curve
from xmatch.ai.vectors import PgArrayStore

from .test_matching import FakeEmbedder, m2  # noqa: F401  (fixture)
from .test_runner import q

pytestmark = pytest.mark.integration


@pytest.fixture
def pairs_ready(m2):  # noqa: F811
    m2.retrieval.similarity_floor = -1.0   # keep every top-K pair so there is enough to label
    run_embed(m2, FakeEmbedder(64), PgArrayStore())
    run_retrieve(m2, PgArrayStore())
    return m2


def scripted(answers):
    it = iter(answers)
    return lambda prompt: next(it)


def test_label_session_writes_append_only_cases(pairs_ready):
    s = pairs_ready
    with psycopg.connect(s.database_url, row_factory=dict_row) as conn:
        pairs = next_pairs(conn, s, 10, -1, 1)
        assert len(pairs) >= 3 and pairs[0]["similarity"] >= pairs[-1]["similarity"]
        out = []
        counts = label_session(conn, s, pairs[:3], scripted([
            "c", "1", "same deadline and source", "",            # confirm
            "m", "r", "3", "Pro vs base model", "iPhone 18 Pro",  # 'more' then reject
            "s",                                                  # skip
        ]), out.append, holdout_size=0)
        assert counts == {"must_confirm": 1, "must_reject": 1, "skipped": 1}
        rows = conn.execute("SELECT label, failure_mode, split, deciding_clause FROM eval_cases ORDER BY id").fetchall()
        assert [(r["label"], r["failure_mode"], r["split"]) for r in rows] == [
            ("must_confirm", "same_date_diff_phrasing", "dev"), ("must_reject", "sibling", "dev")]
        assert rows[0]["deciding_clause"] == "(see reason)"
        # labeled pairs are not offered again
        assert len(next_pairs(conn, s, 10, -1, 1)) == len(pairs) - 2


def test_holdout_split_is_deterministic_and_capped():
    refs = [f"kalshi:K{i}|polymarket:P{i}" for i in range(300)]
    splits = [choose_split(r, 0, 20, 3) for r in refs]
    assert splits == [choose_split(r, 0, 20, 3) for r in refs]
    assert 60 < splits.count("holdout") < 140
    assert choose_split(refs[0], 20, 20, 3) == "dev"


def test_recall_curve_counts_labeled_positive(pairs_ready):
    s = pairs_ready
    with psycopg.connect(s.database_url, row_factory=dict_row) as conn:
        conn.execute("""INSERT INTO eval_cases (pair_ref, label, reason, deciding_clause, split,
                                                kalshi_native_id, polymarket_native_id)
                        VALUES ('kalshi:FED|polymarket:1','must_confirm','same','x','dev','FED','1'),
                               ('kalshi:GONE|polymarket:9','must_confirm','missing','x','dev','GONE','9')""")
        res = recall_curve(conn, s, floors=[0.0, 0.99], k=2)
    assert res.missing == ["kalshi:GONE|polymarket:9"]          # counts against recall, not dropped
    assert res.points[0].must_confirm == 2 and res.points[0].found == 1 and res.points[0].recall == 0.5
    assert res.points[1].candidates <= res.points[0].candidates
