"""Build platform sources with their read-only HTTP clients."""

from __future__ import annotations

from contextlib import contextmanager

from xmatch.config import Settings
from xmatch.http import ReadOnlyClient
from xmatch.ingest.kalshi import KalshiSource
from xmatch.ingest.polymarket import PolymarketSource

PLATFORMS = ("kalshi", "polymarket")


@contextmanager
def open_source(platform: str, s: Settings, on_retry=None):
    pcfg = getattr(s, platform)
    with ReadOnlyClient(pcfg.base_url, s.http, pcfg.requests_per_second, on_retry=on_retry) as client:
        cls = {"kalshi": KalshiSource, "polymarket": PolymarketSource}[platform]
        yield cls(client, pcfg)
