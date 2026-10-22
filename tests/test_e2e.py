"""End to end: two mock supplier sites on localhost, searched through the real API.

Shop A is open and fetched over plain HTTP. Shop B shows prices only to a
logged-in dealer and is fetched with the real browser, exercising the login
flow. Needs Chromium: set BROWSER_EXECUTABLE, or have Playwright's browser installed.
"""

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
import yaml
from fastapi.testclient import TestClient

SHOP_A = """<html><body>{cards}</body></html>"""
CARD_A = """<div class="p"><a href="/item/{id}">{title}</a><span class="price">₪{price}</span>
<span class="st">{stock}</span><span class="mpn">{mpn}</span></div>"""
A_PRODUCTS = [
    ("1", "Kingston SSD 1TB NV3 NVMe", "249", "במלאי", "SNV3S/1000G"),
    ("2", "Samsung SSD 2TB 990 Pro", "799", "אזל", "MZ-V9P2T0BW"),
]

B_LOGIN = """<html><body><form method="post" action="/login">
<input id="user" name="user"><input id="pass" name="pass" type="password">
<button id="go" type="submit">כניסה</button></form></body></html>"""
B_RESULTS = """<html><body>{logout}<ul>
<li class="row"><h4><a href="/x/9">Kingston NV3 1TB SSD</a></h4><em>מק"ט: SNV3S-1000G</em>{price}</li>
</ul></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, status=200, headers=None):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query).get("q", [""])[0].lower()
        if u.path == "/a/search":
            if parse_qs(u.query).get("page", ["1"])[0] != "1":
                return self._send(SHOP_A.format(cards=""))
            cards = "".join(CARD_A.format(id=i, title=t, price=p, stock=s, mpn=m)
                            for i, t, p, s, m in A_PRODUCTS if q in t.lower())
            return self._send(SHOP_A.format(cards=cards))
        if u.path == "/b/login":
            return self._send(B_LOGIN)
        if u.path == "/b/search":
            dealer = "dealer=1" in (self.headers.get("Cookie") or "")
            return self._send(B_RESULTS.format(
                logout='<a class="logout" href="#">יציאה</a>' if dealer else "",
                price='<b class="pr">212.00</b>' if dealer else ""))
        self._send("not found", 404)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        form = parse_qs(body)
        if form.get("user") == ["dealer"] and form.get("pass") == ["s3cret"]:
            return self._send("", 302, {"Location": "/b/search?q=", "Set-Cookie": "dealer=1; Path=/"})
        self._send(B_LOGIN, 200)


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


@pytest.fixture(scope="module")
def client(server, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("cfg")
    os.environ["SUPSEARCH_DATA"] = str(tmp / "data")
    sites = {"settings": {"channel": "", "timeout": 20}, "sites": [
        {"id": "a", "name": "חנות א", "fetch": "http", "vat_included": True,
         "search_url": server + "/a/search?q={query}", "page_url": server + "/a/search?q={query}&page={page}",
         "pages": 3, "item": "div.p",
         "fields": {"title": "a", "url": "a@href", "price": ".price", "stock": ".st", "mpn": ".mpn"}},
        {"id": "b", "name": "חנות ב", "fetch": "browser", "vat_included": False,
         "search_url": server + "/b/search?q={query}", "item": "li.row",
         "login": {"url": server + "/b/login", "username": "#user", "password": "#pass",
                   "submit": "#go", "logged_in": "a.logout"},
         "fields": {"title": "h4 a", "url": "h4 a@href", "price": ".pr",
                    "mpn": {"sel": "em", "re": r":\s*(\S+)"}}},
    ]}
    (tmp / "sites.yaml").write_text(yaml.safe_dump(sites, allow_unicode=True), encoding="utf-8")
    (tmp / "credentials.yaml").write_text(yaml.safe_dump({"b": {"username": "dealer", "password": "s3cret"}}))
    from supsearch import config, fetch
    config.DATA_DIR = fetch.DATA_DIR = str(tmp / "data")
    from supsearch.app import create_app
    with TestClient(create_app(str(tmp / "sites.yaml"), str(tmp / "credentials.yaml")),
                    client=("127.0.0.1", 50000)) as c:  # the office PC itself: open mode
        yield c


def test_sites_listing(client):
    r = client.get("/api/sites")
    assert r.status_code == 200
    assert [s["id"] for s in r.json()] == ["a", "b"]
    assert r.json()[1]["has_credentials"] is True


def test_search_both_sites_with_login(client):
    r = client.post("/api/search", json={"query": "ssd", "sort": "price_asc"})
    assert r.status_code == 200, r.text
    j = r.json()
    status = {s["site"]: s for s in j["sites"]}
    # shop A's Samsung SSD is marked "אזל": dropped at the source, counted only
    assert status["a"]["ok"] and status["a"]["count"] == 1 and status["a"]["out_of_stock"] == 1, status
    assert all("Samsung" not in p["title"] for p in j["items"])
    assert status["b"]["ok"] and status["b"]["count"] == 1, status
    titles = [p["title"] for p in j["items"]]
    assert titles[0] == "Kingston SSD 1TB NV3 NVMe"   # 249 inc VAT
    b = next(p for p in j["items"] if p["site"] == "b")
    assert b["price_ex_vat"] == 212.0 and b["price_inc_vat"] == 250.16  # dealer price, logged in
    assert {f["key"] for f in j["facets"]} >= {"site", "brand", "storage", "stock"}


def test_compare_groups_same_part_number(client):
    j = client.post("/api/search", json={"query": "ssd", "view": "compare", "sort": "price_asc"}).json()
    kingston = next(g for g in j["groups"] if g["key"] == "mpn:SNV3S1000G")
    assert kingston["sites"] == 2
    assert [o["site"] for o in kingston["offers"]] == ["a", "b"]
    assert all(s["cached"] for s in j["sites"])  # second search came from cache


def test_filters_through_api(client):
    j = client.post("/api/search", json={"query": "ssd", "in_stock": True, "facets": {"site": ["חנות א"]}}).json()
    assert [p["title"] for p in j["items"]] == ["Kingston SSD 1TB NV3 NVMe"]


def test_empty_query_rejected(client):
    assert client.post("/api/search", json={"query": "  "}).status_code == 400


def test_healthz_needs_no_login(client):
    assert client.get("/healthz").status_code == 200  # Fly health checks


def test_credentials_from_env(monkeypatch, tmp_path):
    from supsearch.config import load_credentials
    f = tmp_path / "c.yaml"
    f.write_text("a: {username: file, password: x}\nb: {username: fileb, password: y}\n")
    monkeypatch.setenv("SUPPLIER_CREDENTIALS", "b: {username: env, password: z}")
    creds = load_credentials(str(f))
    assert creds["a"]["username"] == "file" and creds["b"]["username"] == "env"


def test_info_reports_egress(monkeypatch, client):
    info = client.get("/api/info").json()
    assert (info["egress"], info["egress_detail"]) == ("direct", "")
    monkeypatch.setenv("EGRESS", "office:office-pc")
    info = client.get("/api/info").json()
    assert (info["egress"], info["egress_detail"]) == ("office", "office-pc")


def test_http_retries_a_dead_pooled_connection():
    """After a Fly suspend/resume the first request on an old connection fails."""
    import asyncio
    import httpx
    from supsearch.fetch import HttpFetcher

    calls = []

    def handler(request):
        calls.append(request.url)
        if len(calls) == 1:
            raise httpx.ReadError("connection reset by peer")
        return httpx.Response(200, text="ok")

    async def go():
        f = HttpFetcher()
        site = {"id": "s"}
        f._clients["s"] = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        assert await f.get(site, "https://s.test/x", 5) == "ok"
        await f.close()
    asyncio.run(go())
    assert len(calls) == 2
