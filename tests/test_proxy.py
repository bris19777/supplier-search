"""Supplier traffic through an outbound proxy (on Fly: Tailscale, exiting at the office).

The mock shops are only reachable under made-up host names that the test proxy
resolves, so a result proves the request really went through the proxy.
"""

import threading
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest
import yaml
from fastapi.testclient import TestClient

from test_e2e import Handler as ShopHandler


def _serve(handler):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


@pytest.fixture(scope="module")
def shop():
    httpd = _serve(ShopHandler)
    yield httpd.server_port
    httpd.shutdown()


@pytest.fixture(scope="module")
def proxy(shop):
    seen = []

    class Proxy(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _forward(self):
            u = urlsplit(self.path)  # a proxy gets the absolute URL
            seen.append(u.hostname)
            body = self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
            conn = HTTPConnection("127.0.0.1", shop, timeout=10)
            headers = {k: v for k, v in self.headers.items() if k.lower() not in ("proxy-connection", "connection")}
            conn.request(self.command, (u.path or "/") + ("?" + u.query if u.query else ""), body or None, headers)
            r = conn.getresponse()
            data = r.read()
            self.send_response(r.status)
            for k, v in r.getheaders():
                if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = _forward

    httpd = _serve(Proxy)
    yield f"http://127.0.0.1:{httpd.server_port}", seen
    httpd.shutdown()


def make_client(tmp_path, monkeypatch, proxy_url, shop_port):
    monkeypatch.setenv("OUTBOUND_PROXY", proxy_url)
    sites = {"settings": {"channel": "", "timeout": 15}, "sites": [
        {"id": "a", "name": "א", "fetch": "http", "search_url": "http://shop-a.test/a/search?q={query}",
         "item": "div.p", "fields": {"title": "a", "url": "a@href", "price": ".price"}},
        {"id": "b", "name": "ב", "fetch": "browser", "search_url": "http://shop-b.test/b/search?q={query}",
         "item": "li.row", "login": {"optional": True, "url": "http://shop-b.test/b/login",
                                     "username": "#user", "password": "#pass", "submit": "#go",
                                     "logged_in": "a.logout"},
         "fields": {"title": "h4 a", "price": ".pr"}},
        {"id": "c", "name": "ג", "fetch": "http", "direct": True,
         "search_url": f"http://127.0.0.1:{shop_port}/a/search?q={{query}}",
         "item": "div.p", "fields": {"title": "a"}},
    ]}
    (tmp_path / "sites.yaml").write_text(yaml.safe_dump(sites, allow_unicode=True), encoding="utf-8")
    (tmp_path / "cred.yaml").write_text(yaml.safe_dump({"b": {"username": "dealer", "password": "s3cret"}}))
    from supsearch import config, fetch
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(fetch, "DATA_DIR", str(tmp_path / "data"))
    from supsearch.app import create_app
    return TestClient(create_app(str(tmp_path / "sites.yaml"), str(tmp_path / "cred.yaml")))


def test_all_supplier_traffic_goes_through_proxy(tmp_path, monkeypatch, proxy, shop):
    url, seen = proxy
    with make_client(tmp_path, monkeypatch, url, shop) as c:
        j = c.post("/api/search", json={"query": "ssd"}).json()
    status = {s["site"]: s for s in j["sites"]}
    assert status["a"]["count"] == 2 and not status["a"]["error"], status
    assert status["b"]["count"] == 1 and not status["b"]["needs_login"], status  # logged in via proxy
    assert status["c"]["count"] == 2, status          # direct: true skips the proxy
    assert {"shop-a.test", "shop-b.test"} <= set(seen)
    assert "127.0.0.1" not in seen


def test_office_down_message(tmp_path, monkeypatch, shop):
    with make_client(tmp_path, monkeypatch, "http://127.0.0.1:9", shop) as c:  # nothing listens
        j = c.post("/api/search", json={"query": "ssd", "sites": ["a", "b", "c"]}).json()
    status = {s["site"]: s for s in j["sites"]}
    assert "מחשב המשרד" in status["a"]["error"]
    assert "מחשב המשרד" in status["b"]["error"]
    assert status["c"]["count"] == 2  # a direct site still works
