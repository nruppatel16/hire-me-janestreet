"""Export a static snapshot of the dashboard for hosting (Vercel), with no database or secrets exposed (D-021).

The same index.html runs in "static mode": it reads data/*.json instead of the live /api endpoints.
Left out on purpose:
- error tracebacks (they contain local file paths)
- raw API payloads (about 55 MB; available in the local dashboard)
- markets outside the eligible / held-out sets (about 300k long-tail rows)
"""

from __future__ import annotations

import json
import shutil
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from xmatch.config import Settings
from xmatch.dashboard.server import STATIC, Api, _json_default

SHARDS = 256
VERCEL_JSON = {
    "cleanUrls": True,
    "headers": [
        {"source": "/(.*)", "headers": [
            {"key": "X-Content-Type-Options", "value": "nosniff"},
            {"key": "Referrer-Policy", "value": "no-referrer"},
            {"key": "Content-Security-Policy",
             "value": "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'"},
        ]},
        {"source": "/data/(.*)", "headers": [{"key": "Cache-Control", "value": "public, max-age=60"}]},
    ],
}


def _dump(path: Path, data: Any) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(data, default=_json_default, separators=(",", ":"))
    path.write_text(raw)
    return len(raw)


def export_site(s: Settings, out: Path, max_pairs: int = 3000) -> dict[str, Any]:
    api = Api(s)
    if out.exists():
        shutil.rmtree(out)
    data = out / "data"
    sizes: dict[str, int] = {}

    summary = api.summary()
    summary["snapshot_at"] = datetime.now(timezone.utc).isoformat()
    sizes["summary"] = _dump(data / "summary.json", summary)
    sizes["volume"] = _dump(data / "volume.json", api.volume_histogram())
    sizes["holdouts"] = _dump(data / "holdouts.json", api.holdouts())
    runs = api.runs(limit=60)
    sizes["runs"] = _dump(data / "runs.json", runs)
    errors = []
    for r in runs:
        if r["error_count"]:
            for e in api.run_errors(r["run_id"]):
                errors.append({"run_id": r["run_id"], "stage": e["stage"], "native_id": e["native_id"],
                               "message": e["message"].split("\n")[0][:300], "detail": None})
    sizes["errors"] = _dump(data / "errors.json", errors)
    sizes["pairs"] = _dump(data / "pairs.json", api.pairs(limit=max_pairs)["rows"])

    with api._conn() as c:
        rows = c.execute("""
            SELECT id, platform, native_id, title, event_title, volume_usd, volume_raw, volume_unit, close_time_utc,
                   source_tz, status, source_status, listed, passes_volume_gate, matchable, holdout_reason,
                   needs_embedding, updated_at, outcome_labels, content_hash, hash_version, first_seen_run,
                   last_seen_run, resolution_text
            FROM markets
            WHERE listed AND (NOT matchable OR (status = 'open' AND passes_volume_gate))""").fetchall()
    listing, shards = [], defaultdict(dict)
    for r in rows:
        listing.append({k: r[k] for k in ("id", "platform", "native_id", "title", "event_title", "volume_usd",
                                          "close_time_utc", "status", "listed", "passes_volume_gate", "matchable",
                                          "holdout_reason", "needs_embedding", "updated_at")})
        shards[r["id"] % SHARDS][str(r["id"])] = {**r, "payload_json": None, "payload_run_id": r["last_seen_run"],
                                                  "payload_fetched_at": None}
    sizes["markets"] = _dump(data / "markets.json", listing)
    sizes["detail_shards"] = sum(_dump(data / "m" / f"{k}.json", v) for k, v in shards.items())

    html = (STATIC / "index.html").read_text().replace('<html lang="en">', '<html lang="en" data-static="1">', 1)
    (out / "index.html").write_text(html)
    (out / "vercel.json").write_text(json.dumps(VERCEL_JSON, indent=2))
    return {"out": str(out), "markets": len(listing), "pairs": min(max_pairs, summary["later"]["candidate_pairs"]),
            "bytes": sum(sizes.values()), "sizes": sizes}
