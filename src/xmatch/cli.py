"""xmatch command line. All commands are read-only with respect to the platforms."""

from __future__ import annotations

import argparse
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


def cmd_dashboard(args) -> int:
    from xmatch.dashboard.server import serve
    serve(port=args.port)
    return 0


def main(argv=None) -> int:
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
