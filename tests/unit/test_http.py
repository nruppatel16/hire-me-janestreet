import httpx
import pytest

from xmatch.config import HttpCfg
from xmatch.http import FetchError, ReadOnlyClient

CFG = HttpCfg(timeout_s=5, max_retries=3, backoff_base_s=1, backoff_max_s=8, user_agent="t")


def make(handler, sleeps, rps=1000.0):
    return ReadOnlyClient("https://x.test", CFG, rps, transport=httpx.MockTransport(handler),
                          sleep=sleeps.append)


def test_retries_429_and_5xx_then_succeeds():
    seq = iter([429, 503, 200])
    def handler(req):
        code = next(seq)
        return httpx.Response(code, json={"ok": True} if code == 200 else {})
    sleeps = []
    c = make(handler, sleeps)
    assert c.get_json("/m") == {"ok": True}
    assert c.request_count == 3
    backoffs = [s for s in sleeps if s >= 0.5]
    assert len(backoffs) == 2 and backoffs[1] >= 1.0  # exponential: attempt 1 in [1,2]


def test_gives_up_after_max_retries():
    sleeps = []
    c = make(lambda req: httpx.Response(500), sleeps)
    with pytest.raises(FetchError) as ei:
        c.get_json("/m")
    assert ei.value.attempts == 4 and ei.value.status == 500


def test_non_retryable_status_fails_fast():
    c = make(lambda req: httpx.Response(404, text="nope"), [])
    with pytest.raises(FetchError) as ei:
        c.get_json("/m")
    assert ei.value.attempts == 1


def test_network_errors_are_retried():
    calls = {"n": 0}
    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json=[1])
    assert make(handler, []).get_json("/m") == [1]


def test_backoff_is_capped():
    c = make(lambda r: httpx.Response(200, json={}), [])
    assert all(c._backoff(a) <= CFG.backoff_max_s for a in range(20))


def test_client_is_get_only():
    assert not any(hasattr(ReadOnlyClient, m) for m in ("post", "put", "delete", "patch"))


def test_provider_honors_google_retry_delay():
    from xmatch.http import ProviderClient
    seq = iter([429, 200])
    def handler(req):
        if next(seq) == 429:
            return httpx.Response(429, json={"error": {"details": [
                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "7s"}]}})
        return httpx.Response(200, json={"ok": 1})
    sleeps = []
    c = ProviderClient("https://x.test", CFG, 1000.0, transport=httpx.MockTransport(handler), sleep=sleeps.append)
    assert c.post_json("/e", {}) == {"ok": 1}
    assert max(sleeps) >= 7.0


def test_ollama_embedder_shape_check(monkeypatch):
    from xmatch.ai.embeddings import OllamaEmbedder
    from xmatch.config import load_settings
    from xmatch.http import FetchError, ProviderClient
    monkeypatch.setenv("DATABASE_URL", "postgresql://x@localhost/y")
    s = load_settings()
    s.embeddings.dims = 3
    def handler(req):
        import json as _j
        n = len(_j.loads(req.content)["input"])
        return httpx.Response(200, json={"embeddings": [[1.0, 0, 0]] * n, "prompt_eval_count": 7})
    e = OllamaEmbedder(s, ProviderClient("http://o", CFG, 1000, transport=httpx.MockTransport(handler), sleep=lambda x: None))
    r = e.embed(["a", "b"])
    assert len(r.vectors) == 2 and r.tokens_in == 7
    s.embeddings.dims = 4
    e2 = OllamaEmbedder(s, ProviderClient("http://o", CFG, 1000, transport=httpx.MockTransport(handler), sleep=lambda x: None))
    with pytest.raises(FetchError):
        e2.embed(["a"])
