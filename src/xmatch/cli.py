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
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
