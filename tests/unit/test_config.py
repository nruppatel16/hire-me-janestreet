from xmatch.config import load_settings


def test_loads_thresholds_from_toml(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x@localhost/y")
    s = load_settings()
    assert s.volume_gate.min_volume_usd == 10000
    assert s.kalshi.mve_filter == "exclude"
    assert s.database_url == "postgresql://x@localhost/y"


def test_missing_database_url_fails(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr("xmatch.config.load_dotenv", lambda *a, **k: None)
    import pytest
    with pytest.raises(RuntimeError):
        load_settings()
