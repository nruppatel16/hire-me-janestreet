from decimal import Decimal as D

import psycopg
import pytest

from xmatch.ingest.runner import IngestRun
from xmatch.pricing.engine import kalshi_books
from xmatch.pricing.job import paper_cash, run_pricing

from .test_matching import PolySource
from .test_runner import FakeSource, mk, q

pytestmark = pytest.mark.integration


def pm(i, q_="Will the Fed cut in December?", outcomes='["Yes","No"]', end="2027-01-01T00:00:00Z"):
    return {"id": i, "question": q_, "description": "Resolves YES if so.", "endDate": end, "outcomes": outcomes,
            "volumeNum": 50000, "active": True, "closed": False, "clobTokenIds": f'["y{i}","n{i}"]',
            "conditionId": f"0xc{i}", "feesEnabled": False}


class FakeBooks:
    def __init__(self):
        self.k = {}          # ticker -> (yes_bids, no_bids)
        self.p = {}          # token -> asks
        self.k_final = {}    # ticker -> market dict
        self.p_res = {}      # condition -> resolution row

    def kalshi_books(self, tickers):
        return {t: kalshi_books(*self.k[t]) for t in tickers if t in self.k}

    def polymarket_asks(self, token):
        return self.p.get(token, [])

    def kalshi_fee_multiplier(self, series):
        return D(1)

    def kalshi_market(self, t):
        return self.k_final.get(t, {"status": "active"})

    def polymarket_resolution(self, cond):
        return self.p_res.get(cond)


@pytest.fixture
def priced(cfg):
    IngestRun(FakeSource([[mk("FED"), mk("BTC")]]), cfg).run()
    IngestRun(PolySource([[pm(1), pm(2, outcomes='["Up","Down"]')]]), cfg).run()
    with psycopg.connect(cfg.database_url) as c:
        for k, p in (("FED", "1"), ("BTC", "2")):
            c.execute("""INSERT INTO eval_cases (pair_ref,label,reason,deciding_clause,split,kalshi_native_id,
                         polymarket_native_id) VALUES (%s,'must_confirm','same','x','dev',%s,%s)""", (f"{k}|{p}", k, p))
    b = FakeBooks()
    # direction 1: Kalshi YES ask 0.47 (NO bid 0.53) + Poly NO ask 0.40 = 0.87 -> arb
    b.k["FED"] = ([(D("0.40"), D(500))], [(D("0.53"), D(500))])
    b.p["y1"], b.p["n1"] = [(D("0.65"), D(500))], [(D("0.40"), D(500))]
    b.k["BTC"] = ([(D("0.40"), D(500))], [(D("0.53"), D(500))])
    return cfg, b


def test_unverified_kalshi_fee_blocks_paper_trades(priced):
    cfg, b = priced
    assert cfg.fees.kalshi_coefficient is None
    st = run_pricing(cfg, b)
    assert st.status == "succeeded" and st.pairs == 2
    assert st.by_status == {"fee_unverified": 1, "no_arb": 1, "direction_unverified": 2}
    assert st.opened == 0 and q(cfg.database_url, "SELECT count(*) n FROM paper_positions")[0]["n"] == 0
    # the gross arb is still observed, so lifetimes are measurable
    assert q(cfg.database_url, "SELECT count(*) n FROM arb_observations WHERE open")[0]["n"] == 1


def test_paper_trade_opens_once_and_respects_budget(priced):
    cfg, b = priced
    cfg.fees.kalshi_coefficient = 0.07
    st = run_pricing(cfg, b)
    assert st.arbs == 1 and st.opened == 1
    pos = q(cfg.database_url, "SELECT * FROM paper_positions")[0]
    assert pos["direction"] == 1 and pos["cost_usd"] <= D(cfg.paper.max_per_trade_usd)
    assert pos["fees_usd"] > 0 and pos["pair_source"] == "human"
    assert run_pricing(cfg, b).opened == 0          # still open -> no duplicate
    assert paper_cash(psycopg.connect(cfg.database_url, row_factory=psycopg.rows.dict_row), cfg) == \
        D(cfg.paper.bankroll_usd) - pos["cost_usd"]


def test_arb_window_closes_when_prices_move(priced):
    cfg, b = priced
    cfg.fees.kalshi_coefficient = 0.07
    run_pricing(cfg, b)
    b.p["n1"] = [(D("0.60"), D(500))]   # 0.47 + 0.60 > 1
    run_pricing(cfg, b)
    obs = q(cfg.database_url, "SELECT open, first_seen <= last_seen ok FROM arb_observations")
    assert obs == [{"open": False, "ok": True}]


def test_settlement_uses_platform_results(priced):
    cfg, b = priced
    cfg.fees.kalshi_coefficient = 0.07
    run_pricing(cfg, b)
    b.k_final["FED"] = {"status": "finalized", "result": "yes", "settlement_value_dollars": "1.0000"}
    run_pricing(cfg, b)                                   # Polymarket not resolved yet -> stays open
    assert q(cfg.database_url, "SELECT status FROM paper_positions")[0]["status"] == "open"
    b.p_res["0xc1"] = {"status": "resolved", "payouts": [1000000, 0]}
    st = run_pricing(cfg, b)
    pos = q(cfg.database_url, "SELECT * FROM paper_positions")[0]
    assert st.settled == 1 and pos["status"] == "settled"
    assert pos["payout_usd"] == pos["contracts"] and pos["pnl_usd"] == pos["contracts"] - pos["cost_usd"] > 0
    assert pos["settle_note"] is None


def test_settlement_exposes_a_false_positive(priced):
    cfg, b = priced
    cfg.fees.kalshi_coefficient = 0.07
    run_pricing(cfg, b)
    b.k_final["FED"] = {"status": "finalized", "result": "no", "settlement_value_dollars": "0.0000"}
    b.p_res["0xc1"] = {"status": "resolved", "payouts": [1000000, 0]}   # Poly said YES, Kalshi said NO
    run_pricing(cfg, b)
    pos = q(cfg.database_url, "SELECT * FROM paper_positions")[0]
    assert pos["payout_usd"] == 0 and pos["pnl_usd"] == -pos["cost_usd"]
    assert "NOT equivalent" in pos["settle_note"]


def test_closing_soon_is_skipped(cfg):
    IngestRun(FakeSource([[mk("FED")]]), cfg).run()
    IngestRun(PolySource([[pm(1, end="2020-01-01T00:00:00Z")]]), cfg).run()
    with psycopg.connect(cfg.database_url) as c:
        c.execute("""INSERT INTO eval_cases (pair_ref,label,reason,deciding_clause,split,kalshi_native_id,
                     polymarket_native_id) VALUES ('x','must_confirm','same','x','dev','FED','1')""")
    st = run_pricing(cfg, FakeBooks())
    assert st.by_status == {"closing": 2}


def test_rejected_or_relabelled_pairs_are_not_priced(priced):
    cfg, b = priced
    with psycopg.connect(cfg.database_url) as c:
        c.execute("""INSERT INTO eval_cases (pair_ref,label,reason,deciding_clause,split,kalshi_native_id,
                     polymarket_native_id) VALUES ('FED|1','must_reject','changed my mind','x','dev','FED','1')""")
    assert run_pricing(cfg, b).pairs == 1   # latest label wins
