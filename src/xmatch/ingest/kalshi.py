"""Kalshi source: list open markets (GET /markets) and normalize them. Read-only, public endpoints."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from xmatch.config import KalshiCfg
from xmatch.http import ReadOnlyClient
from xmatch.models import NormalizedMarket, clean_text, normalize_ws, parse_utc, to_decimal

PLATFORM = "kalshi"

# Market.status enum per docs -> normalized lifecycle status.
STATUS_MAP = {
    "active": "open",
    "closed": "closed",
    "determined": "closed",
    "disputed": "closed",
    "amended": "closed",
    "finalized": "resolved",
    "initialized": "unknown",
    "inactive": "unknown",
}


def compose_title(event_title: str | None, market_title: str | None) -> str | None:
    """Event title + market title (D-008). Market titles often omit event context."""
    et, mt = normalize_ws(event_title), normalize_ws(market_title)
    if et and mt and et.lower() != mt.lower():
        return f"{et} — {mt}"
    return (mt or et) or None


def normalize(raw: dict[str, Any], event: dict[str, Any] | None, cfg: KalshiCfg) -> NormalizedMarket:
    event = event or {}
    volume = to_decimal(raw.get("volume_fp"))
    notional = to_decimal(raw.get("notional_value_dollars"))
    # D-004: dollar volume = contracts traded x dollar value per contract at settlement.
    volume_usd = volume * notional if volume is not None and notional is not None else None
    return NormalizedMarket(
        platform=PLATFORM,
        native_id=raw["ticker"],
        event_id=raw.get("event_ticker"),
        event_title=normalize_ws(event.get("title")) or None,
        title=compose_title(event.get("title"), raw.get("title")),
        description=clean_text(event.get("sub_title"), raw.get("yes_sub_title"), sep=" · "),
        resolution_text=clean_text(raw.get("rules_primary"), raw.get("rules_secondary")),
        close_time_utc=parse_utc(raw.get("close_time")),
        source_tz=cfg.source_tz,
        outcome_labels=["Yes", "No"],  # Kalshi contracts are YES/NO for binary and scalar markets
        volume_raw=volume,
        volume_unit="contracts",
        volume_usd=volume_usd,
        status=STATUS_MAP.get(raw.get("status") or "", "unknown"),
        source_status=raw.get("status"),
    )


class KalshiSource:
    platform = PLATFORM

    def __init__(self, client: ReadOnlyClient, cfg: KalshiCfg):
        self.client, self.cfg = client, cfg
        self.events: dict[str, dict[str, Any]] = {}

    # Raw payloads are envelopes of two untouched API objects, {"market": ..., "event": ...}, because a normalized
    # Kalshi row depends on both (event title, D-008). This keeps every row traceable to exactly what produced it.

    @staticmethod
    def native_id(raw: dict[str, Any]) -> str:
        return raw["market"]["ticker"]

    def _paginate(self, path: str, key: str, params: dict[str, Any]) -> Iterator[list[dict]]:
        cursor, seen_cursors = "", set()
        while True:
            body = self.client.get_json(path, params={**params, **({"cursor": cursor} if cursor else {})})
            items = body.get(key) or []
            yield items
            cursor = body.get("cursor") or ""
            if not cursor or not items:
                return
            if cursor in seen_cursors:
                raise RuntimeError(f"{path}: pagination cursor repeated")
            seen_cursors.add(cursor)

    def prepare(self) -> None:
        """Load open events for titles (D-008)."""
        for page in self._paginate("/events", "events", {"status": "open", "limit": self.cfg.events_page_limit}):
            for ev in page:
                self.events[ev["event_ticker"]] = ev

    def _fill_missing_events(self, page: list[dict]) -> None:
        missing = sorted({m.get("event_ticker") for m in page if m.get("event_ticker")} - self.events.keys())
        for i in range(0, len(missing), 100):
            batch = missing[i:i + 100]
            body = self.client.get_json("/events", params={"tickers": ",".join(batch), "limit": len(batch)})
            for ev in body.get("events") or []:
                self.events[ev["event_ticker"]] = ev
            for t in batch:  # don't refetch events the API doesn't return
                self.events.setdefault(t, {})

    def pages(self) -> Iterator[list[dict]]:
        params = {"status": "open", "limit": self.cfg.markets_page_limit, "mve_filter": self.cfg.mve_filter}
        for page in self._paginate("/markets", "markets", params):
            self._fill_missing_events(page)
            yield [{"market": m, "event": self._event_without_markets(m)} for m in page]

    def _event_without_markets(self, m: dict[str, Any]) -> dict[str, Any] | None:
        ev = self.events.get(m.get("event_ticker") or "")
        return {k: v for k, v in ev.items() if k != "markets"} if ev else None

    def normalize(self, raw: dict[str, Any]) -> NormalizedMarket:
        return normalize(raw["market"], raw.get("event"), self.cfg)

    def reference_count(self) -> int:
        """Independent enumeration for Gate 1 (D-006): open markets nested in GET /events."""
        tickers: set[str] = set()
        params = {"status": "open", "limit": self.cfg.events_page_limit, "with_nested_markets": "true"}
        for page in self._paginate("/events", "events", params):
            for ev in page:
                for m in ev.get("markets") or []:
                    if m.get("status") == "active":
                        tickers.add(m["ticker"])
        return len(tickers)
