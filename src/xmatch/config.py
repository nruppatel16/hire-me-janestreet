"""Config loading: thresholds and endpoints from config/settings.toml, secrets from the environment."""

from __future__ import annotations

import os
import tomllib
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "config" / "settings.toml"


class HttpCfg(BaseModel):
    timeout_s: float
    max_retries: int
    backoff_base_s: float
    backoff_max_s: float
    user_agent: str


class KalshiCfg(BaseModel):
    base_url: str
    markets_page_limit: int
    events_page_limit: int
    mve_filter: str
    requests_per_second: float
    source_tz: str


class PolymarketCfg(BaseModel):
    base_url: str
    markets_page_limit: int
    events_page_limit: int
    requests_per_second: float
    source_tz: str


class VolumeGateCfg(BaseModel):
    min_volume_usd: float


class IngestCfg(BaseModel):
    raw_payload_keep_runs: int
    hash_version: int
    stale_run_minutes: int


class Gate1Cfg(BaseModel):
    count_tolerance_pct: float


class ProviderCfg(BaseModel):
    base_url: str
    api_key_env: str
    requests_per_second: float


class EmbeddingsCfg(BaseModel):
    provider: str
    model: str
    dims: int
    batch_size: int
    text_version: int
    price_per_mtok_usd: float
    price_version: str


class RetrievalCfg(BaseModel):
    top_k: int
    similarity_floor: float
    block_rows: int


class Settings(BaseModel):
    http: HttpCfg
    kalshi: KalshiCfg
    polymarket: PolymarketCfg
    volume_gate: VolumeGateCfg
    ingest: IngestCfg
    gate1: Gate1Cfg
    providers: dict[str, ProviderCfg]
    embeddings: EmbeddingsCfg
    retrieval: RetrievalCfg
    database_url: str

    def api_key(self, provider: str) -> str:
        env = self.providers[provider].api_key_env
        key = os.environ.get(env, "").strip()
        if not key:
            raise RuntimeError(f"{env} is not set (put it in .env)")
        return key


def load_settings(path: Path | None = None, database_url: str | None = None) -> Settings:
    load_dotenv(ROOT / ".env", override=False)
    path = Path(os.environ.get("XMATCH_CONFIG", path or DEFAULT_CONFIG))
    with open(path, "rb") as f:
        data = tomllib.load(f)
    dsn = database_url or os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set (copy .env.example to .env)")
    return Settings(**data, database_url=dsn)


@lru_cache
def settings() -> Settings:
    return load_settings()
