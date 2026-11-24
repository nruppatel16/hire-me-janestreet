"""Normalized market schema (PRD DP-2) plus the content hash (DP-4) and the volume gate (DP-8)."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

_WS = re.compile(r"\s+")

# Fields that must be present for a market to enter matching (PRD DP-2 / data quality rules).
REQUIRED_FOR_MATCHING = ("title", "resolution_text", "close_time_utc")


@dataclass
class NormalizedMarket:
    platform: str
    native_id: str
    title: str | None
    resolution_text: str | None
    close_time_utc: datetime | None
    source_tz: str
    status: str                 # open | closed | resolved | unknown
    source_status: str | None
    volume_raw: Decimal | None
    volume_unit: str            # contracts | usd
    volume_usd: Decimal | None
    event_id: str | None = None
    event_title: str | None = None
    description: str | None = None
    outcome_labels: list[str] = field(default_factory=list)

    @property
    def holdout_reason(self) -> str | None:
        missing = [f for f in REQUIRED_FOR_MATCHING if _blank(getattr(self, f))]
        return f"missing:{','.join(missing)}" if missing else None

    @property
    def matchable(self) -> bool:
        return self.holdout_reason is None


def _blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def normalize_ws(s: str | None) -> str:
    return _WS.sub(" ", s or "").strip()


def content_hash(title: str | None, resolution_text: str | None, version: int) -> str:
    """sha256 over whitespace-normalized title + resolution text. Prices and volume are excluded,
    so only substantive text changes trigger re-embedding and re-classification."""
    blob = f"v{version}\x1f{normalize_ws(title)}\x1f{normalize_ws(resolution_text)}"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def passes_volume_gate(volume_usd: Decimal | None, min_volume_usd: float) -> bool:
    return volume_usd is not None and volume_usd >= Decimal(str(min_volume_usd))


def parse_utc(value: Any) -> datetime | None:
    """Parse an RFC3339 / ISO-8601 timestamp to an aware UTC datetime. Returns None if unparseable."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None  # refuse to guess a timezone
    return dt.astimezone(timezone.utc)


def to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def clean_text(*parts: str | None, sep: str = "\n\n") -> str | None:
    kept = [p.strip() for p in parts if p and p.strip()]
    return sep.join(kept) if kept else None
