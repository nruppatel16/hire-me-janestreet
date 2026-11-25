"""Local read-only dashboard: a stdlib HTTP server that serves one static page plus JSON endpoints.

Binds to 127.0.0.1 only. Every database session is opened read-only, so the dashboard cannot modify the store.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from xmatch.ai.matching import retrieval_version
from xmatch.config import Settings, load_settings
from xmatch.db import connect

STATIC = Path(__file__).parent / "static"
log = logging.getLogger(__name__)

PLATFORMS = ("kalshi", "polymarket")
SORTS = {
    "volume": "volume_usd DESC NULLS LAST, id",
    "close": "close_time_utc ASC NULLS LAST, id",
    "updated": "updated_at DESC, id",
}
FILTERS = {
    "live": "listed AND status = 'open'",
    "gated": "listed AND status = 'open' AND passes_volume_gate AND matchable",
    "heldout": "listed AND NOT matchable",
    "unlisted": "NOT listed",
    "all": "true",
}


def _json_default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    raise TypeError(type(o).__name__)


class Api:
    """Query layer. Each call uses a fresh read-only connection."""

    def __init__(self, settings: Settings):
        self.s = settings

    def _conn(self):
        conn = connect(self.s.database_url, autocommit=True)
        conn.execute("SET default_transaction_read_only = on")
        conn.execute("SET statement_timeout = '15s'")
        return conn

    def summary(self) -> dict[str, Any]:
        with self._conn() as c:
            by_platform = {r["platform"]: r for r in c.execute("""
                SELECT platform,
                       count(*) FILTER (WHERE listed AND status = 'open') AS open_listed,
                       count(*) FILTER (WHERE listed AND status = 'open' AND passes_volume_gate) AS gated,
                       count(*) FILTER (WHERE listed AND status = 'open' AND passes_volume_gate AND matchable) AS eligible,
                       count(*) FILTER (WHERE listed AND status = 'open' AND passes_volume_gate AND matchable
                                        AND needs_embedding) AS pending_embedding,
                       count(*) FILTER (WHERE listed AND NOT matchable) AS held_out,
                       count(*) FILTER (WHERE NOT listed) AS unlisted,
                       count(*) AS total
                FROM markets GROUP BY platform""")}
            last = {}
            for p in PLATFORMS:
                last[p] = {
                    "last_success": c.execute(
                        "SELECT run_id, ended_at, rows_read FROM ingest_runs WHERE platform=%s AND status='succeeded'"
                        " ORDER BY run_id DESC LIMIT 1", (p,)).fetchone(),
                    # Ignore deliberate Gate 1 forced failures when deciding whether to raise an alert.
                    "last_run": c.execute(
                        "SELECT run_id, status, started_at, ended_at FROM ingest_runs WHERE platform=%s"
                        " AND config_snapshot ->> 'fail_after_pages' IS NULL"
                        " ORDER BY run_id DESC LIMIT 1", (p,)).fetchone(),
                }
            later = c.execute("""
                SELECT (SELECT count(*) FROM candidate_pairs WHERE retrieval_version = %(v)s) AS candidate_pairs,
                       (SELECT count(*) FROM verdicts WHERE verdict = 'equivalent' AND direction_safe) AS confirmed,
                       (SELECT count(*) FROM arb_observations) AS arbs""", {"v": retrieval_version(self.s)}).fetchone()
            emb = c.execute("""
                SELECT (SELECT count(*) FROM eval_cases WHERE label = 'must_confirm') AS must_confirm,
                       (SELECT count(*) FROM eval_cases WHERE label = 'must_reject') AS must_reject,
                       (SELECT coalesce(sum(cost_usd), 0) FROM llm_calls WHERE status = 'ok') AS spend_usd""").fetchone()
            now = c.execute("SELECT now() AS now").fetchone()["now"]
        return {
            "now": now,
            "volume_gate_usd": self.s.volume_gate.min_volume_usd,
            "platforms": {p: {**(by_platform.get(p) or {}), **last[p]} for p in PLATFORMS},
            "later": later,
            "eval": emb,
            "retrieval": {"version": retrieval_version(self.s), "top_k": self.s.retrieval.top_k,
                          "floor": self.s.retrieval.similarity_floor, "model": self.s.embeddings.model},
        }

    def pairs(self, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        v = retrieval_version(self.s)
        with self._conn() as c:
            total = c.execute("SELECT count(*) AS n FROM candidate_pairs WHERE retrieval_version = %s", (v,)).fetchone()["n"]
            rows = c.execute("""
                SELECT c.pair_id, c.similarity, c.rank_k2p, c.rank_p2k,
                       k.id AS k_mid, k.native_id AS k_id, k.title AS k_title, k.close_time_utc AS k_close,
                       p.id AS p_mid, p.native_id AS p_id, p.title AS p_title, p.close_time_utc AS p_close,
                       (SELECT e.label FROM eval_cases e WHERE e.kalshi_native_id = k.native_id
                          AND e.polymarket_native_id = p.native_id ORDER BY e.id DESC LIMIT 1) AS label
                FROM candidate_pairs c JOIN markets k ON k.id = c.kalshi_id JOIN markets p ON p.id = c.polymarket_id
                WHERE c.retrieval_version = %s ORDER BY c.similarity DESC LIMIT %s OFFSET %s""",
                (v, max(1, min(limit, 200)), max(0, offset))).fetchall()
        return {"total": total, "rows": rows}

    def volume_histogram(self) -> list[dict]:
        with self._conn() as c:
            return c.execute("""
                SELECT platform,
                       CASE WHEN volume_usd IS NULL OR volume_usd < 1 THEN -1
                            ELSE least(floor(log(volume_usd))::int, 8) END AS bucket,
                       count(*) AS n
                FROM markets WHERE listed AND status = 'open'
                GROUP BY 1, 2 ORDER BY 1, 2""").fetchall()

    def holdouts(self) -> list[dict]:
        with self._conn() as c:
            return c.execute("""
                SELECT platform, holdout_reason, count(*) AS n
                FROM markets WHERE listed AND NOT matchable
                GROUP BY 1, 2 ORDER BY 3 DESC""").fetchall()

    def runs(self, limit: int = 40) -> list[dict]:
        with self._conn() as c:
            return c.execute("""
                SELECT run_id, platform, status, started_at, ended_at,
                       extract(epoch FROM coalesce(ended_at, now()) - started_at) AS seconds,
                       rows_read, rows_written, rows_new, rows_changed, rows_held_out, error_count,
                       (config_snapshot ->> 'fail_after_pages') IS NOT NULL AS injected
                FROM ingest_runs ORDER BY run_id DESC LIMIT %s""", (limit,)).fetchall()

    def run_errors(self, run_id: int) -> list[dict]:
        with self._conn() as c:
            return c.execute("""
                SELECT id, at, stage, native_id, message, detail
                FROM ingest_errors WHERE run_id = %s ORDER BY id LIMIT 200""", (run_id,)).fetchall()

    def markets(self, q: str = "", platform: str = "", filt: str = "gated", sort: str = "volume",
                limit: int = 50, offset: int = 0) -> dict[str, Any]:
        where = [FILTERS.get(filt, FILTERS["gated"])]
        args: list[Any] = []
        if platform in PLATFORMS:
            where.append("platform = %s")
            args.append(platform)
        if q.strip():
            where.append("(title ILIKE %s OR native_id ILIKE %s OR event_title ILIKE %s)")
            like = "%" + re.sub(r"([%_\\])", r"\\\1", q.strip()) + "%"
            args += [like, like, like]
        sql_where = " AND ".join(where)
        limit = max(1, min(limit, 200))
        with self._conn() as c:
            total = c.execute(f"SELECT count(*) AS n FROM markets WHERE {sql_where}", args).fetchone()["n"]
            rows = c.execute(f"""
                SELECT id, platform, native_id, title, volume_usd, close_time_utc, status, listed,
                       passes_volume_gate, matchable, holdout_reason, needs_embedding, updated_at
                FROM markets WHERE {sql_where}
                ORDER BY {SORTS.get(sort, SORTS['volume'])} LIMIT %s OFFSET %s""",
                args + [limit, max(0, offset)]).fetchall()
        return {"total": total, "rows": rows}

    def market(self, market_id: int) -> dict[str, Any] | None:
        with self._conn() as c:
            return c.execute("""
                SELECT m.*, rp.payload_json, rp.fetched_at AS payload_fetched_at, rp.run_id AS payload_run_id
                FROM markets m JOIN raw_payloads rp ON rp.id = m.raw_payload_id
                WHERE m.id = %s""", (market_id,)).fetchone()


def make_handler(api: Api):
    class Handler(BaseHTTPRequestHandler):
        server_version = "xmatch-dashboard"

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data: Any, code: int = 200) -> None:
            self._send(code, json.dumps(data, default=_json_default).encode(), "application/json")

        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            qs = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                if url.path in ("/", "/index.html"):
                    return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
                if url.path == "/api/summary":
                    return self._json(api.summary())
                if url.path == "/api/volume":
                    return self._json(api.volume_histogram())
                if url.path == "/api/holdouts":
                    return self._json(api.holdouts())
                if url.path == "/api/runs":
                    return self._json(api.runs())
                if m := re.fullmatch(r"/api/runs/(\d+)/errors", url.path):
                    return self._json(api.run_errors(int(m.group(1))))
                if url.path == "/api/pairs":
                    return self._json(api.pairs(limit=int(qs.get("limit", 50)), offset=int(qs.get("offset", 0))))
                if url.path == "/api/markets":
                    return self._json(api.markets(
                        q=qs.get("q", ""), platform=qs.get("platform", ""), filt=qs.get("filter", "gated"),
                        sort=qs.get("sort", "volume"), limit=int(qs.get("limit", 50)),
                        offset=int(qs.get("offset", 0))))
                if m := re.fullmatch(r"/api/markets/(\d+)", url.path):
                    row = api.market(int(m.group(1)))
                    return self._json(row) if row else self._json({"error": "not found"}, 404)
                return self._json({"error": "not found"}, 404)
            except ValueError as e:
                return self._json({"error": f"bad request: {e}"}, 400)
            except Exception as e:  # noqa: BLE001
                log.exception("dashboard error")
                return self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    return Handler


def serve(port: int = 8050, settings: Settings | None = None) -> None:
    api = Api(settings or load_settings())
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(api))
    print(f"xmatch dashboard on http://127.0.0.1:{port}  (read-only, Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
