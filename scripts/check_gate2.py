"""Gate 2 check (Milestone 2: embedding retrieval). Exit 0 = pass, 1 = fail.

  1. Every eligible market (open, listed, over the volume gate, complete) has a current embedding.
  2. Re-running the embed job makes zero provider calls.
  3. The candidate set for the configured retrieval version exists, built after the latest embedding.
  4. Recall on dev must_confirm pairs at the configured K and floor meets the target,
     with at least the minimum number of labeled positives.
  Also reported: candidate count, embedding cost, label counts. A model swap being config-only is covered by
  tests/integration/test_matching.py::test_model_change_is_config_only.
"""

from __future__ import annotations

import sys

from xmatch.ai.embeddings import make_embedder
from xmatch.ai.matching import embedding_key, pending_markets, retrieval_version, run_embed
from xmatch.ai.recall import recall_curve
from xmatch.ai.vectors import ELIGIBLE_SQL, PgArrayStore
from xmatch.config import load_settings
from xmatch.db import connect

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)


def main() -> int:
    s = load_settings()
    key, version = embedding_key(s), retrieval_version(s)
    with connect(s.database_url, autocommit=True) as conn:
        eligible = conn.execute(f"SELECT count(*) AS n FROM markets m WHERE {ELIGIBLE_SQL}").fetchone()["n"]
        pending = len(pending_markets(conn, key))
        check("all eligible markets embedded", pending == 0,
              f"{eligible - pending:,} of {eligible:,} embedded with {key.model}/{key.dims}d/text v{key.text_version}")

    st = run_embed(s, make_embedder(s), PgArrayStore())
    check("re-run makes zero provider calls", st.status == "succeeded" and st.api_calls == 0,
          f"embed run {st.run_id}: pending {st.pending}, api calls {st.api_calls}")

    with connect(s.database_url, autocommit=True) as conn:
        r = conn.execute(
            """SELECT max(ended_at) FILTER (WHERE job = 'retrieve' AND config_snapshot ->> 'version' = %s) AS retrieved,
                      max(ended_at) FILTER (WHERE job = 'embed' AND rows_written > 0) AS embedded
               FROM ingest_runs WHERE status = 'succeeded' AND platform = 'all'""", (version,)).fetchone()
        n_pairs = conn.execute("SELECT count(*) AS n FROM candidate_pairs WHERE retrieval_version = %s",
                               (version,)).fetchone()["n"]
        fresh = r["retrieved"] is not None and (r["embedded"] is None or r["retrieved"] >= r["embedded"])
        check("candidate set current", fresh and n_pairs > 0,
              f"{n_pairs:,} candidate pairs for {version} (retrieved {r['retrieved']}, last embed {r['embedded']})")

        res = recall_curve(conn, s, floors=[s.retrieval.similarity_floor])
        p = res.points[0]
        enough = p.must_confirm >= s.gate2.min_must_confirm
        check(f"recall >= {s.gate2.recall_target:.0%} on >= {s.gate2.min_must_confirm} dev must_confirm pairs",
              enough and p.recall >= s.gate2.recall_target,
              f"{p.found}/{p.must_confirm} = {p.recall:.1%} at K={s.retrieval.top_k}, floor={p.floor:.3f}"
              + ("" if enough else "  (label more pairs: `uv run xmatch label`)"))

        labels = conn.execute("SELECT label, split, count(*) AS n FROM eval_cases GROUP BY 1, 2 ORDER BY 1, 2").fetchall()
        cost = conn.execute("SELECT count(*) AS calls, coalesce(sum(tokens_in), 0) AS tok, coalesce(sum(cost_usd), 0) AS usd "
                            "FROM llm_calls WHERE kind = 'embed' AND status = 'ok'").fetchone()
    print(f"\nlabels: " + (", ".join(f"{l['label']}/{l['split']}={l['n']}" for l in labels) or "none"))
    print(f"embedding spend to date: {cost['calls']} calls, {cost['tok']:,} tokens, ${float(cost['usd']):.4f} "
          f"(list price; free tier is not billed)")
    failed = [n for n, ok, _ in results if not ok]
    print(f"\nGATE 2: {'PASS' if not failed else 'FAIL'} ({len(results) - len(failed)}/{len(results)} checks)")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
