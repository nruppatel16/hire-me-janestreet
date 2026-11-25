"""Read-only HTTP client: GET only, client-side rate limit, retries with exponential backoff and jitter."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import Any

import httpx

from xmatch.config import HttpCfg

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class FetchError(RuntimeError):
    """A request failed after all retries (or with a non-retryable status)."""

    def __init__(self, message: str, *, url: str, status: int | None = None, attempts: int = 0):
        super().__init__(message)
        self.url = url
        self.status = status
        self.attempts = attempts


class _RetryingClient:
    """Shared rate limit + retry machinery. Subclasses decide which HTTP methods are exposed."""

    def __init__(
        self,
        base_url: str,
        cfg: HttpCfg,
        requests_per_second: float,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        on_retry: Callable[[str, int, str], None] | None = None,
        headers: dict[str, str] | None = None,
    ):
        self._client = httpx.Client(
            base_url=base_url,
            timeout=cfg.timeout_s,
            headers={"User-Agent": cfg.user_agent, "Accept": "application/json", **(headers or {})},
            transport=transport,
        )
        self._cfg = cfg
        self._min_interval = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        self._sleep = sleep
        self._clock = clock
        self._last = float("-inf")
        self._on_retry = on_retry
        self.request_count = 0

    def _throttle(self) -> None:
        wait = self._last + self._min_interval - self._clock()
        if wait > 0:
            self._sleep(wait)
        self._last = self._clock()

    def _backoff(self, attempt: int) -> float:
        cap = min(self._cfg.backoff_max_s, self._cfg.backoff_base_s * (2**attempt))
        return random.uniform(cap / 2, cap)  # jitter, never zero

    def _request_json(self, method: str, path: str, **kw: Any) -> Any:
        last_err = ""
        status: int | None = None
        for attempt in range(self._cfg.max_retries + 1):
            self._throttle()
            self.request_count += 1
            try:
                resp = self._client.request(method, path, **kw)
            except httpx.TransportError as e:
                last_err, status = f"{type(e).__name__}: {e}", None
            else:
                if resp.status_code == 200:
                    return resp.json()
                status = resp.status_code
                last_err = f"HTTP {status}: {resp.text[:200]}"
                if status not in RETRYABLE_STATUS:
                    raise FetchError(last_err, url=str(resp.request.url), status=status, attempts=attempt + 1)
            if attempt < self._cfg.max_retries:
                if self._on_retry:
                    self._on_retry(path, attempt + 1, last_err)
                self._sleep(self._backoff(attempt))
        raise FetchError(f"gave up after {self._cfg.max_retries + 1} attempts: {last_err}",
                         url=path, status=status, attempts=self._cfg.max_retries + 1)

    def close(self) -> None:  # noqa: D401
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class ReadOnlyClient(_RetryingClient):
    """Client for the prediction-market platforms. Exposes only GET: there is deliberately no POST/PUT/DELETE,
    so no code path can submit anything to Kalshi or Polymarket."""

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request_json("GET", path, params=params)


class ProviderClient(_RetryingClient):
    """Client for AI providers (embeddings / LLM). POST is needed for inference; it is never pointed at a
    trading platform."""

    def post_json(self, path: str, body: dict[str, Any]) -> Any:
        return self._request_json("POST", path, json=body)
