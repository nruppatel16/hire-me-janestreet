import psycopg
import pytest
from psycopg.rows import dict_row

from xmatch.config import KalshiCfg
from xmatch.http import FetchError
from xmatch.ingest import kalshi
from xmatch.ingest.runner import IngestRun

pytestmark = pytest.mark.integration

KCFG = KalshiCfg(base_url="x", markets_page_limit=2, events_page_limit=2, mve_filter="exclude",
                 requests_per_second=1, source_tz="UTC")


def mk(ticker, vol="20000.00", rules="Resolves YES if it happens.", title="Will it happen?"):
    return {"ticker": ticker, "event_ticker": "EV", "title": title, "rules_primary": rules,
            "rules_secondary": "", "close_time": "2027-01-01T00:00:00Z", "status": "active",
            "volume_fp": vol, "notional_value_dollars": "1.0000", "yes_ask_dollars": "0.5000"}


class FakeSource:
    platform = "kalshi"

    def __init__(self, pages, fail_on_page=None):
        self._pages, self.fail_on_page = pages, fail_on_page

    def prepare(self):
        pass

    def pages(self):
        for i, p in enumerate(self._pages):
            if self.fail_on_page == i:
                raise FetchError("HTTP 503 after retries", url="/markets", status=503, attempts=7)
            yield p

    native_id = staticmethod(lambda raw: raw["ticker"])

    def normalize(self, raw):
        return kalshi.normalize(raw, {"title": "Event"}, KCFG)


def q(dsn, sql, *a):
    with psycopg.connect(dsn, row_factory=dict_row) as c:
        return c.execute(sql, a).fetchall()


def snapshot(dsn):
    return q(dsn, "SELECT native_id, content_hash, title, resolution_text, listed, status, needs_embedding,"
                  " passes_volume_gate FROM markets ORDER BY native_id")


def test_two_runs_identical_counts_no_duplicates(cfg):
    pages = [[mk("A"), mk("B")], [mk("C"), mk("B")]]  # B repeated across pages (not a snapshot)
    s1 = IngestRun(FakeSource(pages), cfg).run()
    snap1 = snapshot(cfg.database_url)
    s2 = IngestRun(FakeSource(pages), cfg).run()
    assert s1.status == s2.status == "succeeded"
    assert s1.rows_read == s2.rows_read == 3 and s1.duplicates_skipped == 1
    assert (s1.rows_new, s2.rows_new, s2.rows_changed) == (3, 0, 0)
    assert snapshot(cfg.database_url) == snap1
    assert q(cfg.database_url, "SELECT count(*) n, count(DISTINCT (platform, native_id)) d FROM markets")[0] == {"n": 3, "d": 3}


def test_only_text_changes_flag_reembedding(cfg):
    IngestRun(FakeSource([[mk("A"), mk("B")]]), cfg).run()
    with psycopg.connect(cfg.database_url) as c:
        c.execute("UPDATE markets SET needs_embedding = false")  # pretend M2 embedded them
    st = IngestRun(FakeSource([[mk("A", vol="50000.00"), mk("B", rules="Resolves YES if it happens by June.")]]), cfg).run()
    assert st.rows_changed == 1
    rows = {r["native_id"]: r["needs_embedding"] for r in snapshot(cfg.database_url)}
    assert rows == {"A": False, "B": True}


def test_volume_gate_flags_not_deletes(cfg):
    IngestRun(FakeSource([[mk("BIG", vol="10000.00"), mk("SMALL", vol="9999.99")]]), cfg).run()
    rows = {r["native_id"]: r["passes_volume_gate"] for r in snapshot(cfg.database_url)}
    assert rows == {"BIG": True, "SMALL": False}


def test_missing_resolution_text_held_out_and_counted(cfg):
    st = IngestRun(FakeSource([[mk("A"), mk("NORULES", rules="")]]), cfg).run()
    assert st.rows_held_out == 1
    r = q(cfg.database_url, "SELECT matchable, holdout_reason, resolution_text FROM markets WHERE native_id='NORULES'")[0]
    assert r == {"matchable": False, "holdout_reason": "missing:resolution_text", "resolution_text": None}


def test_absent_market_marked_unlisted_only_after_success(cfg):
    IngestRun(FakeSource([[mk("A"), mk("B")]]), cfg).run()
    IngestRun(FakeSource([[mk("A")]]), cfg).run()
    rows = {r["native_id"]: r["listed"] for r in snapshot(cfg.database_url)}
    assert rows == {"A": True, "B": False}
    IngestRun(FakeSource([[mk("A"), mk("B")]]), cfg).run()  # B comes back
    assert all(r["listed"] for r in snapshot(cfg.database_url))


def test_forced_mid_run_failure_leaves_store_consistent(cfg):
    good = [[mk("A"), mk("B")], [mk("C"), mk("D")]]
    IngestRun(FakeSource(good), cfg).run()
    before = snapshot(cfg.database_url)
    changed = [[mk("A", rules="new A"), mk("B")], [mk("C", rules="new C"), mk("D")], [mk("E")]]
    st = IngestRun(FakeSource(changed), cfg, fail_after_pages=1).run()

    assert st.status == "failed"
    run = q(cfg.database_url, "SELECT status, ended_at, error_count FROM ingest_runs WHERE run_id=%s", st.run_id)[0]
    assert run["status"] == "failed" and run["ended_at"] is not None and run["error_count"] == 1
    err = q(cfg.database_url, "SELECT stage, message FROM ingest_errors WHERE run_id=%s", st.run_id)[0]
    assert err["stage"] == "run" and "InjectedFailure" in err["message"]

    after = {r["native_id"]: r for r in snapshot(cfg.database_url)}
    assert set(after) == {"A", "B", "C", "D"}                      # E never written, nothing deleted
    assert all(r["listed"] for r in after.values())                 # no sweep on failure
    assert after["A"]["resolution_text"] == "new A"                 # page 1 committed
    assert after["C"] == {r["native_id"]: r for r in before}["C"]   # page 2 rolled back entirely
    # failed page's raw payloads rolled back too
    assert q(cfg.database_url, "SELECT count(*) n FROM raw_payloads WHERE run_id=%s AND native_id='C'", st.run_id)[0]["n"] == 0
    # every market still traces to an existing raw payload whose run_id matches its last_seen_run
    orphans = q(cfg.database_url, """SELECT count(*) n FROM markets m LEFT JOIN raw_payloads rp ON rp.id = m.raw_payload_id
                                     WHERE rp.id IS NULL OR rp.run_id <> m.last_seen_run OR rp.native_id <> m.native_id""")
    assert orphans[0]["n"] == 0

    # next successful run repairs everything
    assert IngestRun(FakeSource(changed), cfg).run().status == "succeeded"
    assert len(snapshot(cfg.database_url)) == 5


def test_fetch_error_mid_run_marks_failed(cfg):
    st = IngestRun(FakeSource([[mk("A")], [mk("B")]], fail_on_page=1), cfg).run()
    assert st.status == "failed" and st.rows_written == 1
    d = q(cfg.database_url, "SELECT detail FROM ingest_errors WHERE run_id=%s", st.run_id)[0]["detail"]
    assert d["status"] == 503 and d["attempts"] == 7


def test_raw_payload_pruning_keeps_referenced(cfg):
    cfg.ingest.raw_payload_keep_runs = 2
    for _ in range(4):
        IngestRun(FakeSource([[mk("A"), mk("B")]]), cfg).run()
    IngestRun(FakeSource([[mk("A")]]), cfg).run()  # B's newest payload is now from an old run
    runs = q(cfg.database_url, "SELECT DISTINCT run_id FROM raw_payloads ORDER BY 1")
    assert len(runs) <= 3
    assert q(cfg.database_url, "SELECT count(*) n FROM markets m JOIN raw_payloads rp ON rp.id=m.raw_payload_id")[0]["n"] == 2


def test_matchable_constraint_enforced_in_db(cfg):
    IngestRun(FakeSource([[mk("A")]]), cfg).run()
    with pytest.raises(psycopg.errors.CheckViolation):
        with psycopg.connect(cfg.database_url) as c:
            c.execute("UPDATE markets SET resolution_text = NULL")
