"""Interactive hand-labeling of candidate pairs into eval_cases (PRD section 7).

Labels are the user's judgement. This tool only shows the evidence and records the answer.
eval_cases is append-only, so a relabel inserts a new row that `supersedes` the old one.
"""

from __future__ import annotations

import getpass
import hashlib
import textwrap
from collections.abc import Callable

from xmatch.ai.matching import retrieval_version
from xmatch.config import Settings

REJECT_MODES = {
    "1": "deadline",          # different deadlines / windows
    "2": "threshold",         # above vs at-or-above, different strike
    "3": "sibling",           # iPhone 18 vs iPhone 18 Pro
    "4": "advances_vs_wins",  # CA-11 jungle primary style
    "5": "definition",        # same topic, different resolution definition / source
    "6": "different_event",   # sounds alike, different event
    "7": "other",
}
CONFIRM_MODES = {"1": "same_date_diff_phrasing", "2": "none"}


def pair_ref(kalshi_native: str, poly_native: str) -> str:
    return f"kalshi:{kalshi_native}|polymarket:{poly_native}"


def choose_split(ref: str, holdout_so_far: int, holdout_size: int, every: int) -> str:
    """Deterministic: roughly 1 in `every` cases goes to the frozen holdout until it holds `holdout_size`."""
    if holdout_so_far >= holdout_size:
        return "dev"
    return "holdout" if int(hashlib.sha256(ref.encode()).hexdigest(), 16) % every == 0 else "dev"


def next_pairs(conn, s: Settings, limit: int, min_sim: float, max_sim: float) -> list[dict]:
    """Unlabeled candidate pairs of the current retrieval version, most similar first."""
    return conn.execute(
        """SELECT c.pair_id, c.similarity, c.rank_k2p, c.rank_p2k,
                  k.native_id AS k_id, k.title AS k_title, k.resolution_text AS k_rules, k.close_time_utc AS k_close,
                  k.volume_usd AS k_vol, k.outcome_labels AS k_out,
                  p.native_id AS p_id, p.title AS p_title, p.resolution_text AS p_rules, p.close_time_utc AS p_close,
                  p.volume_usd AS p_vol, p.outcome_labels AS p_out
           FROM candidate_pairs c
           JOIN markets k ON k.id = c.kalshi_id JOIN markets p ON p.id = c.polymarket_id
           WHERE c.retrieval_version = %s AND c.similarity BETWEEN %s AND %s
             AND NOT EXISTS (SELECT 1 FROM eval_cases e
                             WHERE e.kalshi_native_id = k.native_id AND e.polymarket_native_id = p.native_id)
           ORDER BY c.similarity DESC LIMIT %s""",
        (retrieval_version(s), min_sim, max_sim, limit)).fetchall()


def _side(name: str, nid: str, title: str, close, vol, outcomes, rules: str, full: bool, width: int) -> str:
    body = rules or "(no resolution text)"
    if not full and len(body) > 700:
        body = body[:700] + " …[m for more]"
    wrapped = "\n".join(textwrap.fill(par, width, initial_indent="    ", subsequent_indent="    ")
                        for par in body.split("\n") if par.strip())
    return (f"  {name}  {nid}\n  {title}\n  closes {close:%Y-%m-%d %H:%M} UTC · volume ${float(vol or 0):,.0f}"
            f" · outcomes {' / '.join(outcomes or [])}\n{wrapped}")


def label_session(conn, s: Settings, pairs: list[dict], ask: Callable[[str], str], say: Callable[[str], None],
                  holdout_size: int = 20, holdout_every: int = 3, width: int = 96) -> dict[str, int]:
    counts = {"must_confirm": 0, "must_reject": 0, "skipped": 0}
    holdout = conn.execute("SELECT count(*) AS n FROM eval_cases WHERE split = 'holdout'").fetchone()["n"]
    user = getpass.getuser()
    for i, pr in enumerate(pairs, 1):
        full = False
        while True:
            say("\n" + "=" * width)
            say(f"[{i}/{len(pairs)}] similarity {pr['similarity']:.3f} · rank K→P {pr['rank_k2p'] or '-'} · "
                f"P→K {pr['rank_p2k'] or '-'}")
            say(_side("KALSHI", pr["k_id"], pr["k_title"], pr["k_close"], pr["k_vol"], pr["k_out"], pr["k_rules"], full, width))
            say(_side("POLYMARKET", pr["p_id"], pr["p_title"], pr["p_close"], pr["p_vol"], pr["p_out"], pr["p_rules"], full, width))
            say("\nWould both pay the same side in EVERY realistic outcome?")
            a = ask("[c]onfirm  [r]eject  [s]kip  [m]ore text  [q]uit > ").strip().lower()
            if a == "m":
                full = True
                continue
            break
        if a == "q":
            break
        if a not in ("c", "r"):
            counts["skipped"] += 1
            continue
        label = "must_confirm" if a == "c" else "must_reject"
        modes = CONFIRM_MODES if a == "c" else REJECT_MODES
        menu = "  ".join(f"{k}) {v}" for k, v in modes.items())
        mode = modes.get(ask(f"failure mode: {menu} > ").strip(), "other" if a == "r" else "none")
        reason = ""
        while not reason:
            reason = ask("one-line reason (required) > ").strip()
        clause = ask("deciding clause (paste the phrase that decides it, Enter to skip) > ").strip() or "(see reason)"
        ref = pair_ref(pr["k_id"], pr["p_id"])
        split = choose_split(ref, holdout, holdout_size, holdout_every)
        holdout += split == "holdout"
        conn.execute(
            """INSERT INTO eval_cases (pair_ref, label, reason, deciding_clause, failure_mode, split,
                                       kalshi_native_id, polymarket_native_id, labeled_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (ref, label, reason, clause, mode, split, pr["k_id"], pr["p_id"], user))
        conn.commit()
        counts[label] += 1
        say(f"saved: {label} ({mode}), split={split}")
    return counts
