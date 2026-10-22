"""Stopping a search: the work on the server stops, not just the browser's wait."""

import asyncio
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace as NS

import pytest
import yaml
from fastapi.testclient import TestClient

from supsearch import ai


@pytest.fixture()
def slow_shop():
    """A supplier that takes 5 s to answer; records requests that ran to the end."""
    finished = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            time.sleep(5)
            body = b'<div class="p"><a href="/1">Slow product</a></div>'
            try:
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                finished.append(self.path)
            except OSError:
                pass  # the caller hung up: that's the point

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}", finished
    httpd.shutdown()


def make_client(tmp_path, monkeypatch, url, env=None):
    from supsearch import app as appmod, config, fetch
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(fetch, "DATA_DIR", str(tmp_path / "data"))
    for k in ("APP_PASSWORD", "ADMIN_PASSWORD", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    sites = {"settings": {"channel": "", "timeout": 20},
             "index": {"queries": ["a", "b", "c"], "pause_seconds": 0},
             "sites": [{"id": "s", "name": "S", "fetch": "http", "search_url": url + "/search?q={query}",
                        "item": "div.p", "fields": {"title": "a", "url": "a@href"}}]}
    (tmp_path / "sites.yaml").write_text(yaml.safe_dump(sites), encoding="utf-8")
    return TestClient(appmod.create_app(str(tmp_path / "sites.yaml"), str(tmp_path / "none.yaml")),
                      client=("127.0.0.1", 50000))


def in_background(fn):
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", fn()))
    t.start()
    return t, out


def test_stop_search_cancels_the_scraping(tmp_path, monkeypatch, slow_shop):
    url, finished = slow_shop
    with make_client(tmp_path, monkeypatch, url) as c:
        started = time.time()
        t, out = in_background(lambda: c.post("/api/search", json={"query": "x", "source": "live",
                                                                     "search_id": "abc"}))
        time.sleep(0.8)
        assert c.post("/api/search/cancel", json={"search_id": "nope"}).json() == {"cancelled": False}
        assert c.post("/api/search/cancel", json={"search_id": "abc"}).json() == {"cancelled": True}
        t.join(10)
        assert out["r"].status_code == 499 and out["r"].json()["detail"] == "החיפוש הופסק"
        assert time.time() - started < 3          # didn't wait for the 5 s supplier
        time.sleep(5)
        assert finished == []                     # the request to the supplier was dropped
        # a stopped search leaves nothing behind: the next one starts clean
        assert c.post("/api/search/cancel", json={"search_id": "abc"}).json() == {"cancelled": False}


def test_stop_ai_search_stops_the_conversation(tmp_path, monkeypatch, slow_shop):
    url, _ = slow_shop
    cancelled = []

    class Hanging:
        def __init__(self):
            self.beta = NS(messages=NS(create=self.create))

        async def create(self, **kw):
            try:
                await asyncio.sleep(60)          # Claude "thinking"
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

    monkeypatch.setattr(ai, "make_client", lambda key: Hanging())
    with make_client(tmp_path, monkeypatch, url, env={"ANTHROPIC_API_KEY": "k"}) as c:
        t, out = in_background(lambda: c.post("/api/ai/search", json={"query": "x", "search_id": "ai1"}))
        time.sleep(0.5)
        assert c.post("/api/search/cancel", json={"search_id": "ai1"}).json()["cancelled"]
        t.join(10)
        assert out["r"].status_code == 499 and cancelled == [True]


def test_stop_index_update(tmp_path, monkeypatch, slow_shop):
    url, _ = slow_shop
    with make_client(tmp_path, monkeypatch, url) as c:
        assert c.post("/api/admin/index/stop").json()["message"] == "אין עדכון שרץ כעת"
        c.post("/api/admin/index/run")
        time.sleep(0.5)
        assert c.get("/api/admin/index").json()["status"]["running"]
        assert "הופסק" in c.post("/api/admin/index/stop").json()["message"]
        for _ in range(50):
            st = c.get("/api/admin/index").json()["status"]
            if not st["running"]:
                break
            time.sleep(0.1)
        assert not st["running"] and "הופסק ידנית" in st["errors"]
