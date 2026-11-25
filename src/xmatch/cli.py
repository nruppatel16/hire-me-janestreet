"""xmatch command line. All commands are read-only with respect to the platforms."""

from __future__ import annotations

import argparse
import signal
import logging
import sys

from xmatch.config import load_settings
from xmatch.migrate import migrate
from xmatch.ingest.runner import IngestRun
from xmatch.sources import PLATFORMS, open_source

log = logging.getLogger("xmatch")


def cmd_migrate(args) -> int:
    applied = migrate(load_settings().database_url)
    print("applied:", ", ".join(applied) if applied else "nothing (up to date)")
    return 0


def run_ingest(platforms, fail_after_pages=None):
    s = load_settings()
    results = {}
    for p in platforms:
        with open_source(p, s, on_retry=lambda path, n, err: log.warning("retry %d %s: %s", n, path, err)) as src:
            results[p] = IngestRun(src, s, fail_after_pages=fail_after_pages).run()
    return results


def cmd_ingest(args) -> int:
    platforms = PLATFORMS if args.platform == "all" else (args.platform,)
    results = run_ingest(platforms, args.fail_after_pages)
    for p, st in results.items():
        print(f"{p:<11} run={st.run_id} status={st.status} pages={st.pages} read={st.rows_read} "
              f"written={st.rows_written} new={st.rows_new} changed={st.rows_changed} "
              f"held_out={st.rows_held_out} dupes_skipped={st.duplicates_skipped} errors={st.error_count}")
    return 0 if all(st.status == "succeeded" for st in results.values()) else 1


def cmd_embed(args) -> int:
    from xmatch.ai.embeddings import make_embedder
    from xmatch.ai.matching import run_embed
    from xmatch.ai.vectors import PgArrayStore
    s = load_settings()
    st = run_embed(s, make_embedder(s), PgArrayStore())
    print(f"embed run={st.run_id} status={st.status} pending={st.pending} embedded={st.embedded} "
          f"api_calls={st.api_calls} tokens_in={st.tokens_in} cost_usd={st.cost_usd:.4f}")
    return 0 if st.status == "succeeded" else 1


def cmd_retrieve(args) -> int:
    from xmatch.ai.matching import run_retrieve
    from xmatch.ai.vectors import PgArrayStore
    st = run_retrieve(load_settings(), PgArrayStore())
    print(f"retrieve run={st.run_id} status={st.status} version={st.version} kalshi={st.kalshi_vectors} "
          f"polymarket={st.polymarket_vectors} pairs={st.pairs}")
    for b in sorted(st.similarity_hist, reverse=True):
        print(f"  sim >= {b}: {st.similarity_hist[b]}")
    return 0 if st.status == "succeeded" else 1


def cmd_match(args) -> int:
    """Slow-cadence job (PRD DP-10): ingest -> embed new/changed -> retrieve."""
    if not args.skip_ingest and cmd_ingest(argparse.Namespace(platform="all", fail_after_pages=None)) != 0:
        return 1
    if cmd_embed(args) != 0:
        return 1
    return cmd_retrieve(args)


def cmd_label(args) -> int:
    from xmatch.ai.labeling import label_session, next_pairs
    from xmatch.db import connect
    s = load_settings()
    with connect(s.database_url) as conn:
        pairs = next_pairs(conn, s, args.n, args.min_sim, args.max_sim)
        if not pairs:
            print("no unlabeled candidate pairs in that similarity range (run `xmatch match` first?)")
            return 0
        try:
            counts = label_session(conn, s, pairs, input, print, s.eval.holdout_size, s.eval.holdout_every)
        except (KeyboardInterrupt, EOFError):
            print("\nstopped; everything labeled so far is saved")
            return 0
    print(f"\nlabeled: {counts}")
    return 0


def cmd_recall(args) -> int:
    from xmatch.ai.recall import recall_curve
    from xmatch.db import connect
    s = load_settings()
    with connect(s.database_url) as conn:
        res = recall_curve(conn, s)
    n = res.points[0].must_confirm if res.points else 0
    print(f"dev must_confirm pairs: {n} (missing a current embedding: {len(res.missing)}), K={s.retrieval.top_k}")
    print(f"{'floor':>6} {'recall':>7} {'found':>6} {'candidates':>11}")
    for p in res.points:
        mark = "  <- configured" if abs(p.floor - s.retrieval.similarity_floor) < 1e-9 else ""
        print(f"{p.floor:>6.3f} {p.recall:>7.1%} {p.found:>6} {p.candidates:>11,}{mark}")
    ok = [p for p in res.points if p.recall >= s.gate2.recall_target]
    if n and ok:
        best = max(ok, key=lambda p: p.floor)
        print(f"highest floor meeting {s.gate2.recall_target:.0%} recall: {best.floor:.3f} "
              f"({best.candidates:,} candidates). Set retrieval.similarity_floor to adopt it.")
    for pp in sorted(res.per_pair, key=lambda x: x["similarity"])[:5]:
        print(f"  hardest: {pp['pair_ref']} sim={pp['similarity']:.3f} ranks K->P {pp['rank_k2p']} P->K {pp['rank_p2k']}")
    return 0


def cmd_dashboard(args) -> int:
    from xmatch.dashboard.server import serve
    serve(port=args.port)
    return 0


def _sigterm_as_interrupt(signum, frame):
    # Schedulers stop jobs with SIGTERM. Turning it into KeyboardInterrupt lets every job's existing
    # handler roll back the open transaction and mark its run 'failed' instead of leaving it 'running'.
    raise KeyboardInterrupt(f"signal {signum}")


def main(argv=None) -> int:
    signal.signal(signal.SIGTERM, _sigterm_as_interrupt)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ap = argparse.ArgumentParser(prog="xmatch", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="apply SQL migrations").set_defaults(fn=cmd_migrate)
    ing = sub.add_parser("ingest", help="full ingest of open markets")
    ing.add_argument("--platform", choices=("all", *PLATFORMS), default="all")
    ing.add_argument("--fail-after-pages", type=int, default=None,
                     help="TESTING ONLY: raise inside the page transaction after N committed pages")
    ing.set_defaults(fn=cmd_ingest)
    sub.add_parser("embed", help="embed new/changed eligible markets").set_defaults(fn=cmd_embed)
    sub.add_parser("retrieve", help="recompute candidate pairs").set_defaults(fn=cmd_retrieve)
    lb = sub.add_parser("label", help="hand-label candidate pairs into the eval set")
    lb.add_argument("--n", type=int, default=20)
    lb.add_argument("--min-sim", type=float, default=0.0)
    lb.add_argument("--max-sim", type=float, default=1.0)
    lb.set_defaults(fn=cmd_label)
    sub.add_parser("recall", help="recall vs candidate count, to choose the similarity floor").set_defaults(fn=cmd_recall)
    mt = sub.add_parser("match", help="slow job: ingest, embed, retrieve")
    mt.add_argument("--skip-ingest", action="store_true")
    mt.set_defaults(fn=cmd_match)
    dash = sub.add_parser("dashboard", help="local read-only dashboard on 127.0.0.1")
    dash.add_argument("--port", type=int, default=8050)
    dash.set_defaults(fn=cmd_dashboard)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
