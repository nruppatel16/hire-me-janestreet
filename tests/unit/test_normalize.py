import json
from datetime import timezone
from decimal import Decimal
from pathlib import Path

import pytest

from xmatch.config import KalshiCfg, PolymarketCfg
from xmatch.ingest import kalshi, polymarket
from xmatch.models import content_hash, parse_utc, passes_volume_gate

FIX = Path(__file__).parents[1] / "fixtures"
KCFG = KalshiCfg(base_url="x", markets_page_limit=1000, events_page_limit=200, mve_filter="exclude",
                 requests_per_second=1, source_tz="UTC")
PCFG = PolymarketCfg(base_url="x", markets_page_limit=100, events_page_limit=100, requests_per_second=1,
                     source_tz="UTC")


def kalshi_raw():
    return json.loads((FIX / "kalshi_markets_page.json").read_text())["markets"]


def poly_raw():
    return json.loads((FIX / "polymarket_markets_page.json").read_text())["markets"]


# ---------- Kalshi ----------

def test_kalshi_real_payload_normalizes():
    raw = kalshi_raw()[1]
    ev = {"title": "Fresno St. vs FGCU total points", "sub_title": "Nov 24"}
    m = kalshi.normalize(raw, ev, KCFG)
    assert m.platform == "kalshi" and m.native_id == raw["ticker"]
    assert m.title.startswith("Fresno St. vs FGCU total points — ")
    assert raw["rules_primary"].strip() in m.resolution_text
    assert m.close_time_utc.tzinfo == timezone.utc
    assert m.volume_unit == "contracts"
    assert m.volume_usd == Decimal(raw["volume_fp"]) * Decimal(raw["notional_value_dollars"])
    assert m.status == "open" and m.matchable


def test_kalshi_title_without_event_falls_back_to_market_title():
    m = kalshi.normalize(kalshi_raw()[0], None, KCFG)
    assert m.title == kalshi_raw()[0]["title"]


def test_kalshi_title_not_duplicated_when_equal():
    assert kalshi.compose_title("Will X happen?", "will x happen?") == "will x happen?"


def test_kalshi_missing_rules_is_held_out_not_guessed():
    raw = {**kalshi_raw()[0], "rules_primary": "", "rules_secondary": "  "}
    m = kalshi.normalize(raw, None, KCFG)
    assert m.resolution_text is None
    assert not m.matchable and m.holdout_reason == "missing:resolution_text"


def test_kalshi_status_mapping():
    for src, want in [("active", "open"), ("finalized", "resolved"), ("determined", "closed"), ("weird", "unknown")]:
        assert kalshi.normalize({**kalshi_raw()[0], "status": src}, None, KCFG).status == want


# ---------- Polymarket ----------

def test_polymarket_real_payload_normalizes():
    raw = poly_raw()[1]
    m = polymarket.normalize(raw, PCFG)
    assert m.native_id == str(raw["id"]) and m.title == raw["question"]
    assert m.resolution_text.startswith(raw["description"].strip()[:40])
    assert m.outcome_labels == ["Yes", "No"]
    assert m.volume_unit == "usd" and m.volume_usd == Decimal(str(raw["volumeNum"]))
    assert m.event_title == raw["events"][0]["title"]
    assert m.status == "open" and m.matchable


def test_polymarket_bad_outcomes_and_dates_hold_out():
    raw = {**poly_raw()[0], "outcomes": "not json", "endDate": None, "description": ""}
    m = polymarket.normalize(raw, PCFG)
    assert m.outcome_labels == []
    assert m.holdout_reason == "missing:resolution_text,close_time_utc"


def test_polymarket_resolution_source_appended():
    m = polymarket.normalize({**poly_raw()[0], "resolutionSource": "https://bls.gov"}, PCFG)
    assert m.resolution_text.endswith("Resolution source: https://bls.gov")


# ---------- shared ----------

def test_content_hash_ignores_whitespace_but_not_words():
    a = content_hash("Will X?", "Resolves  YES if\nX.", 1)
    assert a == content_hash(" Will X? ", "Resolves YES if X.", 1)
    assert a != content_hash("Will X?", "Resolves YES if Y.", 1)
    assert a != content_hash("Will X?", "Resolves YES if X.", 2)  # version bump forces re-embed


def test_content_hash_ignores_price_and_volume():
    raw = kalshi_raw()[1]
    a = kalshi.normalize(raw, None, KCFG)
    b = kalshi.normalize({**raw, "volume_fp": "999999.00", "yes_ask_dollars": "0.9900"}, None, KCFG)
    assert content_hash(a.title, a.resolution_text, 1) == content_hash(b.title, b.resolution_text, 1)


@pytest.mark.parametrize("v,want", [(None, False), (Decimal("9999.99"), False), (Decimal("10000"), True)])
def test_volume_gate(v, want):
    assert passes_volume_gate(v, 10000) is want


def test_parse_utc_refuses_naive_and_garbage():
    assert parse_utc("2026-11-26T19:45:00Z").hour == 19
    assert parse_utc("2026-11-26T19:45:00") is None
    assert parse_utc("garbage") is None


def test_kalshi_source_envelope_carries_event_context():
    src = kalshi.KalshiSource(client=None, cfg=KCFG)
    raw = kalshi_raw()[2]
    env = {"market": raw, "event": {"title": "Prairie View A&M at Iowa", "sub_title": "Nov 28"}}
    assert src.native_id(env) == raw["ticker"]
    m = src.normalize(env)
    assert m.title == "Prairie View A&M at Iowa — Prairie View A&M wins"
    assert m.description.startswith("Nov 28")
    assert src.normalize({"market": raw, "event": None}).title == raw["title"]
