"""Ingest run lifecycle (PRD DP-1, DP-3..DP-8).

Consistency model (D-010):
- `log` connection (autocommit): the ingest_runs row and the ingest_errors rows. These survive any failure.
- `data` connection: one transaction per page, covering raw payloads plus market upserts. A failure rolls back
  only the page in flight, so no market row is ever half-written.
- The `listed` sweep and raw-payload pruning run only after every page succeeded.
"""

from __future__ import annotations

import json
import logging
import traceback
from dataclasses import dataclass, field
from typing import Any, Protocol

import psycopg
from psycopg.types.json import Jsonb

from xmatch.config import Settings
from xmatch.db import connect
from xmatch.http import FetchError
from xmatch.models import NormalizedMarket, content_hash, passes_volume_gate

log = logging.getLogger(__name__)


class Source(Protocol):
    platform: str

    def prepare(self) -> None: ...
    def pages(self): ...
    def native_id(self, raw: dict[str, Any]) -> str: ...
    def normalize(self, raw: dict[str, Any]) -> NormalizedMarket: ...


class InjectedFailure(RuntimeError):
    """Raised by --fail-after-pages to test that a mid-run crash leaves the store consistent."""


@dataclass
class RunStats:
    run_id: int = 0
    status: str = "running"
    pages: int = 0
    rows_read: int = 0
    rows_written: int = 0
    rows_new: int = 0
    rows_changed: int = 0
    rows_held_out: int = 0
    duplicates_skipped: int = 0
    error_count: int = 0
    errored_ids: list[str] = field(default_factory=list)


UPSERT_SQL = """
INSERT INTO markets (
    platform, native_id, event_id, event_title, title, description, resolution_text,
    close_time_utc, source_tz, outcome_labels, volume_raw, volume_unit, volume_usd,
    status, source_status, passes_volume_gate, matchable, holdout_reason,
    content_hash, hash_version, needs_embedding, listed, first_seen_run, last_seen_run, raw_payload_id
) VALUES (
    %(platform)s, %(native_id)s, %(event_id)s, %(event_title)s, %(title)s, %(description)s, %(resolution_text)s,
    %(close_time_utc)s, %(source_tz)s, %(outcome_labels)s, %(volume_raw)s, %(volume_unit)s, %(volume_usd)s,
    %(status)s, %(source_status)s, %(passes_volume_gate)s, %(matchable)s, %(holdout_reason)s,
    %(content_hash)s, %(hash_version)s, true, true, %(run_id)s, %(run_id)s, %(raw_payload_id)s
)
ON CONFLICT (platform, native_id) DO UPDATE SET
    event_id = EXCLUDED.event_id,
    event_title = EXCLUDED.event_title,
    title = EXCLUDED.title,
    description = EXCLUDED.description,
    resolution_text = EXCLUDED.resolution_text,
    close_time_utc = EXCLUDED.close_time_utc,
    source_tz = EXCLUDED.source_tz,
    outcome_labels = EXCLUDED.outcome_labels,
    volume_raw = EXCLUDED.volume_raw,
    volume_unit = EXCLUDED.volume_unit,
    volume_usd = EXCLUDED.volume_usd,
    status = EXCLUDED.status,
    source_status = EXCLUDED.source_status,
    passes_volume_gate = EXCLUDED.passes_volume_gate,
    matchable = EXCLUDED.matchable,
    holdout_reason = EXCLUDED.holdout_reason,
    -- DP-4: flag for re-embedding only when the hashed text (or the hash recipe) changed.
    needs_embedding = markets.needs_embedding
        OR markets.content_hash <> EXCLUDED.content_hash
        OR markets.hash_version <> EXCLUDED.hash_version,
    content_hash = EXCLUDED.content_hash,
    hash_version = EXCLUDED.hash_version,
    listed = true,
    last_seen_run = EXCLUDED.last_seen_run,
    raw_payload_id = EXCLUDED.raw_payload_id,
    updated_at = now()
"""


class IngestRun:
    def __init__(self, source: Source, settings: Settings, fail_after_pages: int | None = None):
        self.source = source
        self.s = settings
        self.fail_after_pages = fail_after_pages
        self.stats = RunStats()
        self.platform = source.platform

    # ---------- run log (autocommit) ----------

    def _fail_abandoned(self, log_conn: psycopg.Connection) -> None:
        """Runs left 'running' by a hard-killed process can never finish themselves; close them out."""
        rows = log_conn.execute(
            """UPDATE ingest_runs SET status = 'failed', ended_at = now(), error_count = error_count + 1
               WHERE platform = %s AND status = 'running' AND started_at < now() - make_interval(mins => %s)
               RETURNING run_id""",
            (self.platform, self.s.ingest.stale_run_minutes),
        ).fetchall()
        for r in rows:
            log_conn.execute(
                "INSERT INTO ingest_errors (run_id, platform, stage, message) VALUES (%s, %s, 'run', %s)",
                (r["run_id"], self.platform,
                 f"abandoned: still 'running' after {self.s.ingest.stale_run_minutes} min (process killed?)"),
            )
            log.warning("%s run %s marked failed (abandoned)", self.platform, r["run_id"])

    def _start(self, log_conn: psycopg.Connection) -> None:
        self._fail_abandoned(log_conn)
        snapshot = {
            "volume_gate": self.s.volume_gate.model_dump(),
            "ingest": self.s.ingest.model_dump(),
            self.platform: getattr(self.s, self.platform).model_dump(),
            "fail_after_pages": self.fail_after_pages,
        }
        row = log_conn.execute(
            "INSERT INTO ingest_runs (job, platform, config_snapshot) VALUES ('ingest', %s, %s) RETURNING run_id",
            (self.platform, Jsonb(snapshot)),
        ).fetchone()
        self.stats.run_id = row["run_id"]

    def _error(self, log_conn, stage: str, message: str, native_id: str | None = None, detail: Any = None):
        self.stats.error_count += 1
        log_conn.execute(
            "INSERT INTO ingest_errors (run_id, platform, native_id, stage, message, detail)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (self.stats.run_id, self.platform, native_id, stage, message[:2000],
             Jsonb(detail) if detail is not None else None),
        )

    def _finish(self, log_conn, status: str) -> None:
        st = self.stats
        st.status = status
        log_conn.execute(
            """UPDATE ingest_runs SET ended_at = now(), status = %s, rows_read = %s, rows_written = %s,
                   rows_new = %s, rows_changed = %s, rows_held_out = %s, error_count = %s
               WHERE run_id = %s""",
            (status, st.rows_read, st.rows_written, st.rows_new, st.rows_changed, st.rows_held_out,
             st.error_count, st.run_id),
        )

    # ---------- data ----------

    def _write_page(self, data_conn: psycopg.Connection, items: list[tuple[dict, NormalizedMarket]]) -> None:
        run_id = self.stats.run_id
        with data_conn.transaction():
            with data_conn.cursor() as cur:
                ids = [m.native_id for _, m in items]
                prev = {
                    r["native_id"]: (r["content_hash"], r["hash_version"])
                    for r in cur.execute(
                        "SELECT native_id, content_hash, hash_version FROM markets"
                        " WHERE platform = %s AND native_id = ANY(%s)",
                        (self.platform, ids),
                    )
                }
                cur.executemany(
                    "INSERT INTO raw_payloads (run_id, platform, native_id, payload_json)"
                    " VALUES (%s, %s, %s, %s) RETURNING id",
                    [(run_id, self.platform, m.native_id, Jsonb(raw)) for raw, m in items],
                    returning=True,
                )
                payload_ids = []
                while True:
                    payload_ids.append(cur.fetchone()["id"])
                    if not cur.nextset():
                        break

                if self.fail_after_pages is not None and self.stats.pages >= self.fail_after_pages:
                    # Fail *inside* the page transaction, after raw payloads were written, to prove rollback.
                    raise InjectedFailure(f"injected failure on page {self.stats.pages + 1}")

                params = []
                for (raw, m), pid in zip(items, payload_ids, strict=True):
                    h = content_hash(m.title, m.resolution_text, self.s.ingest.hash_version)
                    old = prev.get(m.native_id)
                    if old is None:
                        self.stats.rows_new += 1
                    elif old != (h, self.s.ingest.hash_version):
                        self.stats.rows_changed += 1
                    if not m.matchable:
                        self.stats.rows_held_out += 1
                    params.append({
                        **{k: getattr(m, k) for k in (
                            "platform", "native_id", "event_id", "event_title", "title", "description",
                            "resolution_text", "close_time_utc", "source_tz", "volume_raw", "volume_unit",
                            "volume_usd", "status", "source_status", "matchable", "holdout_reason")},
                        "outcome_labels": Jsonb(m.outcome_labels),
                        "passes_volume_gate": passes_volume_gate(m.volume_usd, self.s.volume_gate.min_volume_usd),
                        "content_hash": h,
                        "hash_version": self.s.ingest.hash_version,
                        "run_id": run_id,
                        "raw_payload_id": pid,
                    })
                cur.executemany(UPSERT_SQL, params)
        self.stats.rows_written += len(items)

    def _finalize(self, data_conn: psycopg.Connection) -> None:
        """Only after a complete, successful listing: mark absent markets unlisted (D-009), prune payloads (D-007)."""
        with data_conn.transaction():
            data_conn.execute(
                """UPDATE markets SET listed = false, updated_at = now()
                   WHERE platform = %s AND listed AND last_seen_run <> %s AND NOT (native_id = ANY(%s))""",
                (self.platform, self.stats.run_id, self.stats.errored_ids),
            )
            data_conn.execute(
                """DELETE FROM raw_payloads rp
                   WHERE rp.platform = %s
                     AND rp.run_id NOT IN (SELECT run_id FROM ingest_runs WHERE platform = %s
                                           ORDER BY run_id DESC LIMIT %s)
                     AND NOT EXISTS (SELECT 1 FROM markets m WHERE m.raw_payload_id = rp.id)""",
                (self.platform, self.platform, self.s.ingest.raw_payload_keep_runs),
            )

    # ---------- main ----------

    def run(self) -> RunStats:
        with connect(self.s.database_url, autocommit=True) as log_conn, connect(self.s.database_url) as data_conn:
            self._start(log_conn)
            log.info("%s run %s started", self.platform, self.stats.run_id)
            seen: set[str] = set()
            try:
                self.source.prepare()
                for page in self.source.pages():
                    items: list[tuple[dict, NormalizedMarket]] = []
                    for raw in page:
                        try:
                            nid = self.source.native_id(raw)
                        except Exception as e:  # noqa: BLE001 - malformed row; log and continue
                            self._error(log_conn, "normalize", f"no native id: {e}", detail=_trim(raw))
                            continue
                        if nid in seen:  # Kalshi: "pages are not a snapshot"
                            self.stats.duplicates_skipped += 1
                            continue
                        seen.add(nid)
                        try:
                            items.append((raw, self.source.normalize(raw)))
                        except Exception as e:  # noqa: BLE001
                            self.stats.errored_ids.append(nid)
                            self._error(log_conn, "normalize", f"{type(e).__name__}: {e}", nid, _trim(raw))
                    self.stats.rows_read = len(seen)
                    if items:
                        self._write_page(data_conn, items)
                    self.stats.pages += 1
                    if self.stats.pages % 10 == 0:
                        log.info("%s run %s: %d pages, %d markets", self.platform, self.stats.run_id,
                                 self.stats.pages, self.stats.rows_read)
                self._finalize(data_conn)
            except BaseException as e:
                if not data_conn.closed:
                    data_conn.rollback()
                detail = {"traceback": traceback.format_exc()[-4000:], "pages_committed": self.stats.pages}
                if isinstance(e, FetchError):
                    detail.update(url=e.url, status=e.status, attempts=e.attempts)
                self._error(log_conn, "run", f"{type(e).__name__}: {e}", detail=detail)
                self._finish(log_conn, "failed")
                log.error("%s run %s FAILED: %s", self.platform, self.stats.run_id, e)
                if isinstance(e, KeyboardInterrupt):
                    raise
                return self.stats
            self._finish(log_conn, "succeeded")
            log.info("%s run %s succeeded: %s", self.platform, self.stats.run_id, self.stats)
            return self.stats


def _trim(raw: Any) -> Any:
    s = json.dumps(raw, default=str)
    return raw if len(s) < 4000 else {"truncated": s[:4000]}
