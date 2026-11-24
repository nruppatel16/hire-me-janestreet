"""Gate 1 check (PRD Milestone 1). Exit code 0 = pass, 1 = fail.

Checks:
  1. Idempotency
     a. Replay: re-ingest the exact raw payloads of the latest successful run. The markets table fingerprint must be
        byte-identical, and the row count unchanged.
     b. Live double run: two consecutive live runs give no duplicates. The count delta is reported; it reflects
        markets that opened or closed between runs.
  2. Counts within tolerance of an independent second enumeration of each platform (D-006).
  3. No listed, open, matchable market has a null title, resolution text or close time. Hold-outs are counted.
  4. A forced mid-run failure leaves the store consistent and the run marked failed.

Usage:
  uv run python scripts/check_gate1.py                # runs two fresh live ingests first (slow: ~25 min)
  uv run python scripts/check_gate1.py --reuse-runs   # use the two latest successful runs already in the DB
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

from xmatch.cli import run_ingest
from xmatch.config import load_settings
from xmatch.db import connect
from xmatch.ingest.kalshi import KalshiSource
from xmatch.ingest.polymarket import PolymarketSource
from xmatch.ingest.runner import IngestRun
from xmatch.sources import PLATFORMS, open_source

FINGERPRINT_SQL = """
SELECT count(*) AS n, md5(string_agg(row_to_json(t)::text, '|' ORDER BY t.native_id)) AS fp
FROM (SELECT native_id, event_id, event_title, title, description, resolution_text, close_time_utc, source_tz,
             outcome_labels, volume_raw, volume_unit, volume_usd, status, source_status, passes_volume_gate,
             matchable, holdout_reason, content_hash, hash_version, needs_embedding, listed
      FROM markets WHERE platform = %s) t
"""

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)


def one(conn, sql: str, *args) -> dict[str, Any]:
    return conn.execute(sql, args).fetchone()


class ReplaySource:
    """Feeds the stored raw payloads of a past run back through the platform's real normalizer (no network)."""

    def __init__(self, conn, platform: str, run_id: int, s):
        self.platform, self.conn, self.run_id = platform, conn, run_id
        cls = {"kalshi": KalshiSource, "polymarket": PolymarketSource}[platform]
        self._real = cls(client=None, cfg=getattr(s, platform))  # client unused: no network calls

    def prepare(self):
        pass

    def pages(self):
        with self.conn.cursor(name="replay") as cur:
            cur.itersize = 1000
            cur.execute("SELECT payload_json FROM raw_payloads WHERE run_id = %s ORDER BY id", (self.run_id,))
            batch = []
            for r in cur:
                batch.append(r["payload_json"])
                if len(batch) == 1000:
                    yield batch
                    batch = []
            if batch:
                yield batch

    def native_id(self, raw):
        return self._real.native_id(raw)

    def normalize(self, raw):
        return self._real.normalize(raw)


def latest_runs(conn, platform: str, n: int) -> list[dict]:
    return conn.execute(
        "SELECT * FROM ingest_runs WHERE platform=%s AND status='succeeded' ORDER BY run_id DESC LIMIT %s",
        (platform, n)).fetchall()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reuse-runs", action="store_true")
    ap.add_argument("--fail-after-pages", type=int, default=3)
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = load_settings()
    tol = s.gate1.count_tolerance_pct

    if not args.reuse_runs:
        print("running two live ingests...", flush=True)
        run_ingest(PLATFORMS)
        run_ingest(PLATFORMS)

    with connect(s.database_url, autocommit=True) as conn:
        for p in PLATFORMS:
            print(f"\n=== {p} ===", flush=True)
            runs = latest_runs(conn, p, 2)
            if len(runs) < 2:
                check(f"{p}: two successful runs exist", False, f"found {len(runs)}")
                continue
            b, a = runs  # b = latest

            # ---- 1b. live double run
            dupes = one(conn, "SELECT count(*) - count(DISTINCT native_id) d FROM markets WHERE platform=%s", p)["d"]
            listed_b = one(conn, "SELECT count(*) n FROM markets WHERE platform=%s AND last_seen_run=%s", p, b["run_id"])["n"]
            delta = b["rows_read"] - a["rows_read"]
            pct = 100.0 * abs(delta) / max(a["rows_read"], 1)
            check(f"{p}: live double run, no duplicates", dupes == 0 and listed_b == b["rows_read"],
                  f"run {a['run_id']} read {a['rows_read']}, run {b['run_id']} read {b['rows_read']} "
                  f"(delta {delta:+d}, {pct:.2f}%; new in 2nd run: {b['rows_new']}), duplicate keys: {dupes}")

            # ---- 1a. replay idempotency
            before = one(conn, FINGERPRINT_SQL, p)
            with connect(s.database_url) as rconn:
                rst = IngestRun(ReplaySource(rconn, p, b["run_id"], s), s).run()
            after = one(conn, FINGERPRINT_SQL, p)
            check(f"{p}: replay of run {b['run_id']} is byte-identical",
                  rst.status == "succeeded" and before == after and rst.rows_new == 0 and rst.rows_changed == 0,
                  f"rows {before['n']} -> {after['n']}, fingerprint {before['fp'][:12]} -> {after['fp'][:12]}, "
                  f"replay run {rst.run_id}: read {rst.rows_read}, new {rst.rows_new}, changed {rst.rows_changed}")

            # ---- 2. count vs independent enumeration
            with open_source(p, s) as live:
                ref = live.reference_count()
            stored = b["rows_read"]
            diff_pct = 100.0 * abs(stored - ref) / max(ref, 1)
            check(f"{p}: count within {tol}% of independent listing", diff_pct <= tol,
                  f"stored {stored} vs reference {ref} ({diff_pct:.2f}%)")

            # ---- 3. nulls on active rows
            r = one(conn, """
                SELECT count(*) FILTER (WHERE matchable AND (title IS NULL OR btrim(title)=''
                          OR resolution_text IS NULL OR btrim(resolution_text)='' OR close_time_utc IS NULL)) bad,
                       count(*) FILTER (WHERE NOT matchable) held_out,
                       count(*) FILTER (WHERE resolution_text IS NULL) no_rules,
                       count(*) active
                FROM markets WHERE platform=%s AND listed AND status='open'""", p)
            check(f"{p}: no nulls on active matchable rows", r["bad"] == 0,
                  f"active {r['active']}, bad {r['bad']}, held out {r['held_out']} (no resolution text: {r['no_rules']})")

            # ---- 4. forced failure
            pre = one(conn, FINGERPRINT_SQL, p)
            pre_listed = one(conn, "SELECT count(*) n FROM markets WHERE platform=%s AND listed", p)["n"]
            with open_source(p, s) as live:
                fst = IngestRun(live, s, fail_after_pages=args.fail_after_pages).run()
            run = one(conn, "SELECT * FROM ingest_runs WHERE run_id=%s", fst.run_id)
            errs = one(conn, "SELECT count(*) n, string_agg(message, '; ') m FROM ingest_errors WHERE run_id=%s", fst.run_id)
            post = one(conn, FINGERPRINT_SQL, p)
            post_listed = one(conn, "SELECT count(*) n FROM markets WHERE platform=%s AND listed", p)["n"]
            dupes2 = one(conn, "SELECT count(*) - count(DISTINCT native_id) d FROM markets WHERE platform=%s", p)["d"]
            orphans = one(conn, """SELECT count(*) n FROM markets m LEFT JOIN raw_payloads rp ON rp.id = m.raw_payload_id
                                   WHERE m.platform=%s AND (rp.id IS NULL OR rp.native_id <> m.native_id
                                         OR rp.run_id <> m.last_seen_run)""", p)["n"]
            touched = one(conn, "SELECT count(*) n FROM markets WHERE platform=%s AND last_seen_run=%s", p, fst.run_id)["n"]
            payloads = one(conn, "SELECT count(*) n FROM raw_payloads WHERE run_id=%s", fst.run_id)["n"]
            page_limit = getattr(s, p).markets_page_limit
            ok = (run["status"] == "failed" and run["ended_at"] is not None and errs["n"] >= 1
                  and "InjectedFailure" in (errs["m"] or "")
                  and dupes2 == 0 and orphans == 0
                  and post_listed >= pre_listed          # failure must not unlist anything
                  and post["n"] >= pre["n"]              # nothing deleted
                  and touched == payloads <= args.fail_after_pages * page_limit)  # only committed pages applied
            check(f"{p}: forced failure after {args.fail_after_pages} pages leaves store consistent", ok,
                  f"run {fst.run_id} status={run['status']} errors={errs['n']}; rows {pre['n']}->{post['n']}, "
                  f"listed {pre_listed}->{post_listed}, rows touched by failed run {touched} "
                  f"(= payloads kept {payloads}, <= {args.fail_after_pages}x{page_limit}), "
                  f"duplicates {dupes2}, untraceable rows {orphans}")

    failed = [n for n, ok, _ in results if not ok]
    print(f"\nGATE 1: {'PASS' if not failed else 'FAIL'} ({len(results) - len(failed)}/{len(results)} checks)")
    for n in failed:
        print("  failed:", n)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
