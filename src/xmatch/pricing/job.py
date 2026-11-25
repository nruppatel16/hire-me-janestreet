"""Fast-cadence pricing job (PRD DP-10, section 8) plus the paper ledger (D-024).

One pass:
  1. load tradable pairs (confirmed_pairs view)
  2. fetch order books: Kalshi batch GET, Polymarket per-token GET
  3. price both directions and write quotes
  4. open or close arb observations (PR-8)
  5. paper ledger: open simulated positions on fresh arbs, settle positions whose markets resolved
Read-only toward the platforms: every request is a GET through ReadOnlyClient. Nothing is ever ordered.
"""

from __future__ import annotations

import json
import logging
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from psycopg.types.json import Jsonb

from xmatch.config import Settings
from xmatch.db import connect
from xmatch.http import FetchError, ReadOnlyClient
from xmatch.pricing.engine import Books, FeeModel, Level, kalshi_books, polymarket_books, price_direction

log = logging.getLogger(__name__)
D = Decimal


def _levels(raw: list) -> list[Level]:
    return [(D(str(p)), D(str(s))) for p, s in raw]


class BookSource:
    """All network reads for pricing. Swappable in tests."""

    def __init__(self, s: Settings, kalshi: ReadOnlyClient, clob: ReadOnlyClient, data: ReadOnlyClient):
        self.s, self.kalshi, self.clob, self.data = s, kalshi, clob, data
        self._series: dict[str, D] = {}

    def kalshi_books(self, tickers: list[str]) -> dict[str, Books]:
        out = {}
        n = self.s.pricing.kalshi_books_batch
        for i in range(0, len(tickers), n):
            batch = tickers[i:i + n]
            body = self.kalshi.get_json("/markets/orderbooks", params=[("tickers", t) for t in batch])
            for t, ob in zip(batch, body.get("orderbooks") or [], strict=False):
                fp = (ob.get("orderbook_fp") or {})
                out[ob.get("ticker") or t] = kalshi_books(_levels(fp.get("yes_dollars") or []),
                                                         _levels(fp.get("no_dollars") or []))
        return out

    def polymarket_asks(self, token_id: str) -> list[Level]:
        try:
            book = self.clob.get_json("/book", params={"token_id": token_id})
        except FetchError as e:
            if e.status == 404:   # "No orderbook exists for the requested token id"
                return []
            raise
        return [(D(a["price"]), D(a["size"])) for a in book.get("asks") or []]

    def kalshi_fee_multiplier(self, series_ticker: str | None) -> D | None:
        if not series_ticker:
            return None
        if series_ticker not in self._series:
            ser = self.kalshi.get_json(f"/series/{series_ticker}").get("series") or {}
            ft, mult = ser.get("fee_type"), ser.get("fee_multiplier")
            self._series[series_ticker] = D(str(mult)) if ft and str(ft).startswith("quadratic") and mult is not None else None
        return self._series[series_ticker]

    def kalshi_market(self, ticker: str) -> dict:
        return self.kalshi.get_json(f"/markets/{ticker}").get("market") or {}

    def polymarket_resolution(self, condition_id: str) -> dict | None:
        rows = (self.data.get_json("/v2/resolutions", params={"condition": condition_id}) or {}).get("data") or []
        return rows[0] if rows else None


@dataclass
class PriceStats:
    run_id: int = 0
    status: str = "running"
    pairs: int = 0
    quotes: int = 0
    arbs: int = 0
    opened: int = 0
    settled: int = 0
    by_status: dict[str, int] = field(default_factory=dict)


PAIRS_SQL = """
SELECT cp.kalshi_id, cp.polymarket_id, cp.source,
       k.native_id AS k_ticker, k.close_time_utc AS k_close, k.status AS k_status, k.listed AS k_listed,
       kr.payload_json -> 'event' ->> 'series_ticker' AS k_series,
       p.native_id AS p_id, p.close_time_utc AS p_close, p.status AS p_status, p.listed AS p_listed,
       pr.payload_json AS p_raw
FROM confirmed_pairs cp
JOIN markets k ON k.id = cp.kalshi_id JOIN raw_payloads kr ON kr.id = k.raw_payload_id
JOIN markets p ON p.id = cp.polymarket_id JOIN raw_payloads pr ON pr.id = p.raw_payload_id
"""


def _poly_meta(raw: dict) -> tuple[list[str], list[str], D | None, bool]:
    """(outcomes, token ids, fee rate, fee_known) from the stored Gamma payload."""
    def js(v):
        return json.loads(v) if isinstance(v, str) else (v or [])
    outcomes, tokens = js(raw.get("outcomes")), js(raw.get("clobTokenIds"))
    if raw.get("feesEnabled"):
        rate = (raw.get("feeSchedule") or {}).get("rate")
        return outcomes, tokens, (D(str(rate)) if rate is not None else None), rate is not None
    return outcomes, tokens, D(0), True


def run_pricing(s: Settings, books: BookSource) -> PriceStats:
    st = PriceStats()
    now = datetime.now(timezone.utc)
    min_m = D(str(s.pricing.min_net_margin))
    budget = D(str(s.paper.max_per_trade_usd))
    k_coef = D(str(s.fees.kalshi_coefficient)) if s.fees.kalshi_coefficient is not None else None
    with connect(s.database_url, autocommit=True) as log_conn, connect(s.database_url) as conn:
        st.run_id = log_conn.execute(
            "INSERT INTO ingest_runs (job, platform, config_snapshot) VALUES ('price', 'all', %s) RETURNING run_id",
            (Jsonb({"pricing": s.pricing.model_dump(), "fees": s.fees.model_dump(), "paper": s.paper.model_dump()}),)
        ).fetchone()["run_id"]
        try:
            pairs = conn.execute(PAIRS_SQL).fetchall()
            conn.commit()
            st.pairs = len(pairs)
            kb = books.kalshi_books(sorted({p["k_ticker"] for p in pairs})) if pairs else {}
            for pr in pairs:
                st.quotes += _price_pair(conn, s, books, kb, pr, st, now, min_m, budget, k_coef)
            _settle(conn, s, books, st)
        except BaseException as e:
            conn.rollback()
            log_conn.execute(
                "INSERT INTO ingest_errors (run_id, platform, stage, message, detail) VALUES (%s,'all','price',%s,%s)",
                (st.run_id, f"{type(e).__name__}: {e}"[:2000], Jsonb({"traceback": traceback.format_exc()[-4000:]})))
            log_conn.execute("UPDATE ingest_runs SET status='failed', ended_at=now(), error_count=1, rows_read=%s,"
                             " rows_written=%s WHERE run_id=%s", (st.pairs, st.quotes, st.run_id))
            st.status = "failed"
            if isinstance(e, KeyboardInterrupt):
                raise
            return st
        log_conn.execute("UPDATE ingest_runs SET status='succeeded', ended_at=now(), rows_read=%s, rows_written=%s"
                         " WHERE run_id=%s", (st.pairs, st.quotes, st.run_id))
        st.status = "succeeded"
    return st


def _price_pair(conn, s, books: BookSource, kb, pr, st: PriceStats, now, min_m, budget, k_coef) -> int:
    outcomes, tokens, p_rate, p_fee_known = _poly_meta(pr["p_raw"])
    k_mult = books.kalshi_fee_multiplier(pr["k_series"])
    k_rate = k_coef * k_mult if (k_coef is not None and k_mult is not None) else None
    fee_version = (f"kalshi:{'coef=' + str(k_coef) if k_coef is not None else 'UNVERIFIED'}"
                   f"x{k_mult};poly:rate={p_rate};{s.fees.version_note}")
    k_fee = FeeModel(k_rate, D(str(s.fees.kalshi_round_to)), True)
    p_fee = FeeModel(p_rate, D(str(s.fees.polymarket_round_to)), False)

    base_status = None
    if [o.strip().lower() for o in outcomes] != ["yes", "no"] or len(tokens) != 2:
        base_status = "direction_unverified"   # AI-7: Kalshi YES must map to Polymarket outcome 0 = "Yes"
    elif not (pr["k_listed"] and pr["p_listed"] and pr["k_status"] == "open" and pr["p_status"] == "open"):
        base_status = "closing"
    elif min(pr["k_close"], pr["p_close"]) < now + timedelta(minutes=s.pricing.closing_buffer_minutes):
        base_status = "closing"

    k_books = kb.get(pr["k_ticker"])
    p_books = None
    if base_status is None and k_books is not None:
        p_books = polymarket_books(books.polymarket_asks(tokens[0]), books.polymarket_asks(tokens[1]))
    written = 0
    for direction in (1, 2):
        status = base_status
        q = None
        if status is None:
            if k_books is None or p_books is None:
                status = "no_book"
            else:
                q = price_direction(direction, k_books, p_books, k_fee, p_fee, min_m, budget)
                if q.top_cost is None:
                    status = "no_book"
                elif q.contracts > 0:
                    status = "arb" if (k_rate is not None and p_fee_known) else "fee_unverified"
                else:
                    status = "no_arb"
        st.by_status[status] = st.by_status.get(status, 0) + 1
        row = conn.execute(
            """INSERT INTO quotes (run_id, kalshi_id, polymarket_id, direction, k_ask, p_ask, top_cost, contracts,
                                   cost_usd, fees_usd, net_margin, k_levels, p_levels, fee_version, status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id, quoted_at""",
            (st.run_id, pr["kalshi_id"], pr["polymarket_id"], direction,
             q.k_ask if q else None, q.p_ask if q else None, q.top_cost if q else None,
             q.contracts if q else 0, q.cost if q else None, q.fees if q else None,
             q.net_margin if q else None, q.k_levels if q else None, q.p_levels if q else None,
             fee_version, status)).fetchone()
        written += 1
        _observe(conn, pr, direction, status, q, fee_version, row["quoted_at"])
        if status == "arb":
            st.arbs += 1
            if s.paper.enabled and _open_paper(conn, s, pr, direction, row["id"], k_books, p_books, k_fee, p_fee,
                                               min_m, fee_version):
                st.opened += 1
        conn.commit()
    return written


def _observe(conn, pr, direction, status, q, fee_version, ts) -> None:
    """PR-8: track each arb window from first to last sighting. Gross arbs with unverified fees count too."""
    key = (pr["kalshi_id"], pr["polymarket_id"], direction)
    if status in ("arb", "fee_unverified") and q is not None and q.net_margin is not None:
        upd = conn.execute(
            """UPDATE arb_observations SET last_seen = %s, last_net_margin = %s,
                   best_net_margin = greatest(best_net_margin, %s)
               WHERE kalshi_id = %s AND polymarket_id = %s AND direction = %s AND open""",
            (ts, q.net_margin, q.net_margin, *key))
        if upd.rowcount == 0:
            conn.execute(
                """INSERT INTO arb_observations (kalshi_id, polymarket_id, direction, first_seen, last_seen,
                                                 best_net_margin, last_net_margin, fee_version)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""", (*key, ts, ts, q.net_margin, q.net_margin, fee_version))
    else:
        conn.execute("UPDATE arb_observations SET open = false WHERE kalshi_id=%s AND polymarket_id=%s"
                     " AND direction=%s AND open", key)


def paper_cash(conn, s: Settings) -> D:
    r = conn.execute("""SELECT coalesce(sum(pnl_usd) FILTER (WHERE status = 'settled'), 0) AS realized,
                               coalesce(sum(cost_usd) FILTER (WHERE status = 'open'), 0) AS committed
                        FROM paper_positions""").fetchone()
    return D(str(s.paper.bankroll_usd)) + D(r["realized"]) - D(r["committed"])


def _open_paper(conn, s, pr, direction, quote_id, k_books, p_books, k_fee, p_fee, min_m, fee_version) -> bool:
    exists = conn.execute("SELECT 1 FROM paper_positions WHERE kalshi_id=%s AND polymarket_id=%s AND direction=%s"
                          " AND status='open'", (pr["kalshi_id"], pr["polymarket_id"], direction)).fetchone()
    if exists:
        return False
    budget = min(D(str(s.paper.max_per_trade_usd)), paper_cash(conn, s))
    if budget <= 0:
        return False
    q = price_direction(direction, k_books, p_books, k_fee, p_fee, min_m, budget)
    if q.contracts <= 0:
        return False
    conn.execute(
        """INSERT INTO paper_positions (quote_id, kalshi_id, polymarket_id, direction, pair_source, contracts,
                                        k_cost_usd, p_cost_usd, fees_usd, cost_usd, expected_net_margin, fee_version)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (quote_id, pr["kalshi_id"], pr["polymarket_id"], direction, pr["source"], q.contracts, q.k_cost, q.p_cost,
         q.fees, q.cost, q.net_margin, fee_version))
    log.info("paper: opened %s contracts dir %s on %s / %s, cost $%.2f, margin %.2f%%", q.contracts, direction,
             pr["k_ticker"], pr["p_id"], q.cost, 100 * q.net_margin)
    return True


def _settle(conn, s, books: BookSource, st: PriceStats) -> None:
    """Close positions once BOTH platforms report a final outcome. Payouts come from the platforms, never assumed."""
    rows = conn.execute(
        """SELECT pp.*, k.native_id AS k_ticker, pr.payload_json ->> 'conditionId' AS condition_id
           FROM paper_positions pp JOIN markets k ON k.id = pp.kalshi_id
           JOIN markets p ON p.id = pp.polymarket_id JOIN raw_payloads pr ON pr.id = p.raw_payload_id
           WHERE pp.status = 'open'""").fetchall()
    conn.commit()
    for pos in rows:
        km = books.kalshi_market(pos["k_ticker"])
        if km.get("status") != "finalized":
            continue
        k_yes = (D(str(km["settlement_value_dollars"])) if km.get("settlement_value_dollars") not in (None, "")
                 else D(1) if km.get("result") == "yes" else D(0) if km.get("result") == "no" else None)
        res = books.polymarket_resolution(pos["condition_id"]) if pos["condition_id"] else None
        if k_yes is None or not res or res.get("status") != "resolved" or not res.get("payouts"):
            continue
        p_yes, p_no = (D(str(x)) / D(1_000_000) for x in res["payouts"][:2])   # micro-USDC per share (docs)
        k_per, p_per = (k_yes, p_no) if pos["direction"] == 1 else (D(1) - k_yes, p_yes)
        payout = pos["contracts"] * (k_per + p_per)
        note = None if k_per + p_per == 1 else "legs did not sum to $1: the pair was NOT equivalent"
        conn.execute(
            """UPDATE paper_positions SET status='settled', settled_at=now(), k_payout_per=%s, p_payout_per=%s,
                   payout_usd=%s, pnl_usd=%s, settle_note=%s WHERE id=%s""",
            (k_per, p_per, payout, payout - pos["cost_usd"], note, pos["id"]))
        conn.commit()
        st.settled += 1
