import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import psycopg
import pytest

from xmatch.dashboard.server import Api, make_handler
from xmatch.ingest.runner import IngestRun

from .test_runner import FakeSource, mk

pytestmark = pytest.mark.integration


@pytest.fixture
def seeded(cfg):
    IngestRun(FakeSource([[mk("BIG", vol="50000.00", title="Will 50% of cats nap?"), mk("SMALL", vol="10.00"),
                           mk("NORULES", rules="")]]), cfg).run()
    return cfg


def test_summary_counts(seeded):
    s = Api(seeded).summary()
    k = s["platforms"]["kalshi"]
    assert (k["open_listed"], k["gated"], k["eligible"], k["held_out"]) == (3, 2, 1, 1)
    assert s["platforms"]["kalshi"]["last_success"]["rows_read"] == 3
    assert s["later"] == {"candidate_pairs": 0, "confirmed": 0, "arbs": 0}


def test_market_filters_and_search(seeded):
    api = Api(seeded)
    assert [r["native_id"] for r in api.markets(filt="gated")["rows"]] == ["BIG"]
    assert [r["native_id"] for r in api.markets(filt="heldout")["rows"]] == ["NORULES"]
    assert api.markets(filt="live")["total"] == 3
    assert api.markets(q="50%", filt="all")["total"] == 1  # % is literal, not a wildcard
    assert api.markets(q="%", filt="all")["total"] == 1   # only the title containing a literal %
    assert api.markets(q="_", filt="all")["total"] == 0   # _ is literal too
    assert api.markets(platform="polymarket", filt="all")["total"] == 0


def test_market_detail_includes_raw_payload(seeded):
    api = Api(seeded)
    mid = api.markets(filt="gated")["rows"][0]["id"]
    d = api.market(mid)
    assert d["payload_json"]["ticker"] == "BIG" and d["resolution_text"]
    assert api.market(999999) is None


def test_dashboard_sessions_are_read_only(seeded):
    with Api(seeded)._conn() as c, pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        c.execute("DELETE FROM markets")


def test_http_endpoints(seeded):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Api(seeded)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        html = urllib.request.urlopen(base + "/").read().decode()
        assert "<title>xmatch store</title>" in html
        runs = json.loads(urllib.request.urlopen(base + "/api/runs").read())
        assert runs[0]["status"] == "succeeded"
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + "/api/nope")
        assert e.value.code == 404
    finally:
        httpd.shutdown()


def test_pairs_endpoint_empty_then_populated(seeded):
    api = Api(seeded)
    assert api.pairs() == {"total": 0, "rows": []}
    assert api.summary()["retrieval"]["model"] == seeded.embeddings.model


def test_static_export_has_no_tracebacks_payloads_or_paths(seeded, tmp_path):
    from xmatch.dashboard.export import export_site
    from xmatch.ingest.runner import IngestRun
    from .test_runner import FakeSource
    IngestRun(FakeSource([[mk("X")]], fail_on_page=0), seeded).run()   # a failed run with a traceback
    info = export_site(seeded, tmp_path / "site")
    site = tmp_path / "site"
    assert (site / "index.html").read_text().startswith('<!doctype html>\n<html lang="en" data-static="1">')
    assert json.loads((site / "vercel.json").read_text())["cleanUrls"] is True
    blob = "".join(f.read_text() for f in site.rglob("*.json"))
    assert "Traceback" not in blob and "/Users/" not in blob and '"payload_json":null' in blob
    assert info["markets"] == 2   # BIG (eligible) + NORULES (held out); SMALL is below the gate
    errs = json.loads((site / "data" / "errors.json").read_text())
    assert errs and all(e["detail"] is None for e in errs)


def test_paper_endpoint_on_empty_ledger(seeded):
    d = Api(seeded).paper()
    assert d["account"]["open_n"] == 0 and d["arbs"] == [] and d["confirmed_pairs"] == 0
    assert d["kalshi_fee_verified"] is False
