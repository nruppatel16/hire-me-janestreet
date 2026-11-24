"""Polymarket source: list open markets (Gamma GET /markets/keyset) and normalize them. Read-only."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from xmatch.config import PolymarketCfg
from xmatch.http import ReadOnlyClient
from xmatch.models import NormalizedMarket, clean_text, normalize_ws, parse_utc, to_decimal

PLATFORM = "polymarket"


def parse_json_list(value: Any) -> list[str]:
    """`outcomes` arrives as a JSON-encoded string, e.g. '["Yes", "No"]'."""
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, list):
            return [str(v) for v in parsed]
    return []


def lifecycle_status(raw: dict[str, Any]) -> str:
    if raw.get("closed"):
        return "closed"
    if raw.get("active"):
        return "open"
    return "unknown"


def normalize(raw: dict[str, Any], cfg: PolymarketCfg) -> NormalizedMarket:
    events = raw.get("events") or []
    event = events[0] if events else {}
    volume = to_decimal(raw.get("volumeNum"))
    if volume is None:
        volume = to_decimal(raw.get("volume"))
    source = normalize_ws(raw.get("resolutionSource"))
    return NormalizedMarket(
        platform=PLATFORM,
        native_id=str(raw["id"]),
        event_id=str(event["id"]) if event.get("id") is not None else None,
        event_title=normalize_ws(event.get("title")) or None,
        title=normalize_ws(raw.get("question")) or None,
        description=normalize_ws(raw.get("groupItemTitle")) or None,
        # Rules live in `description` (observed; see docs/api-notes.md).
        resolution_text=clean_text(raw.get("description"), f"Resolution source: {source}" if source else None),
        close_time_utc=parse_utc(raw.get("endDate")),
        source_tz=cfg.source_tz,
        outcome_labels=parse_json_list(raw.get("outcomes")),
        volume_raw=volume,
        volume_unit="usd",
        volume_usd=volume,
        status=lifecycle_status(raw),
        source_status=("closed" if raw.get("closed") else "active" if raw.get("active") else "inactive"),
    )


class PolymarketSource:
    platform = PLATFORM

    def __init__(self, client: ReadOnlyClient, cfg: PolymarketCfg):
        self.client, self.cfg = client, cfg

    @staticmethod
    def native_id(raw: dict[str, Any]) -> str:
        return str(raw["id"])

    def _paginate(self, path: str, key: str, limit: int) -> Iterator[list[dict]]:
        cursor, seen_cursors = None, set()
        while True:
            params: dict[str, Any] = {"closed": "false", "limit": limit}
            if cursor:
                params["after_cursor"] = cursor
            body = self.client.get_json(path, params=params)
            items = body.get(key) or []
            yield items
            cursor = body.get("next_cursor")
            if not cursor or not items:
                return
            if cursor in seen_cursors:
                raise RuntimeError(f"{path}: pagination cursor repeated")
            seen_cursors.add(cursor)

    def prepare(self) -> None:
        pass

    def pages(self) -> Iterator[list[dict]]:
        yield from self._paginate("/markets/keyset", "markets", self.cfg.markets_page_limit)

    def normalize(self, raw: dict[str, Any]) -> NormalizedMarket:
        return normalize(raw, self.cfg)

    def reference_count(self) -> int:
        """Independent enumeration for Gate 1 (D-006): open markets nested in GET /events/keyset.
        Open = closed is false AND active is true. Events also nest inactive placeholder outcomes, which the
        markets listing never returns (D-014)."""
        ids: set[str] = set()
        for page in self._paginate("/events/keyset", "events", self.cfg.events_page_limit):
            for ev in page:
                for m in ev.get("markets") or []:
                    if m.get("closed") is False and m.get("active") is True:
                        ids.add(str(m["id"]))
        return len(ids)
