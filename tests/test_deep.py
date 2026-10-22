"""Deep search: specs read from product pages when the title lacks them."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml
from fastapi.testclient import TestClient

from supsearch.detail import parse_detail, wants_detail
from supsearch.models import Product
from supsearch.normalize import extract_specs

# The three layouts seen on the real sites
TABLE = """<ul id="spec"><li><span>דגם מעבד</span><span>Intel Core™ Ultra 5 225T, 10C</span></li>
<li><span>דור מעבד</span><span>Ultra Series 2</span></li>
<li><span>מקסימום זיכרון</span><span>Up to 64GB (2x 32GB DDR5)</span></li>
<li><span>זיכרון</span><span>16GB DDR5-5600</span></li>
<li><span>אחסון</span><span>512GB SSD M.2 2280 PCIe 4.0</span></li>
<li><span>מערכת הפעלה</span><span>No OS</span></li></ul>"""
LABEL_SPAN = """<ul><li class="row"><span class="badge">גודל מסך </span> 15.6 </li>
<li class="row"><span class="badge">רזולוציית מסך</span> 1920x1080</li>
<li class="row"><span class="badge">חריצי זכרון פנויים</span> 1</li>
<li class="row"><span class="badge">נפח זיכרון</span> 8GB DDR5</li>
<li class="row"><span class="badge">תמיכה במערכות הפעלה</span> Windows 11 64BIT</li></ul>"""
PROSE = """<div id="desc">ThinkPad L16 Gen 3 עם Intel Core Ultra 7 355, זיכרון 32GB DDR5, אחסון 1TB SSD,
מסך 16" WUXGA IPS ו-Windows 11 Pro</div>"""


def test_parse_detail_layouts():
    assert parse_detail(TABLE, {"rows": "#spec li"}) == {
        "cpu": "Core Ultra 5", "ram": "16GB", "storage": "512GB", "os": "ללא מערכת הפעלה"}
    # "free memory slots: 1" and "supported OS" don't pass for RAM / installed OS
    assert parse_detail(LABEL_SPAN, {"rows": "li.row"}) == {"screen": '15.6"', "ram": "8GB"}
    assert parse_detail(PROSE, {"text": "#desc"}) == {
        "cpu": "Core Ultra 7", "ram": "32GB", "storage": "1TB", "screen": '16"', "os": "Windows 11 Pro"}
    assert parse_detail("<p>nothing</p>", {"rows": "li", "text": "#x"}) == {}


@pytest.mark.parametrize("title,expected", [
    ("מחשב נייד 16 אינץ Lenovo ThinkPad L16 Gen 3 (Intel) 21X8002RIV", True),  # no CPU/RAM/SSD
    ("LENOVO M70q G5 i5-14400T | 16GB | 512GB | Dos", False),                 # title has it all
    ("מסך LENOVO ThinkVision S27-4e FHD IPS", False),                          # size from model code
    ("Lenovo L24-4C Monitor", False),
    ("Lenovo ThinkVision monitor FHD", True),                                   # monitor, no size
    ("תיק גב לנייד Lenovo B210 Laptop Backpack", False),                         # accessory
    ("מטען למחשב נייד LENOVO 65W TYPE-C", False),
    ("Kingston SSD 1TB NV3", False),                                            # not a computer
])
def test_wants_detail(title, expected):
    p = Product(site="s", site_name="S", title=title, url="https://x/1", specs=extract_specs(title))
    assert wants_detail(p) is expected


# -- end to end ----------------------------------------------------------------------

SEARCH = """<html><body>
<div class="p"><a href="/item/1">מחשב נייד Lenovo ThinkPad L16 21X8002RIV</a></div>
<div class="p"><a href="/item/2">מחשב נייד Lenovo IdeaPad Slim 3 83US005QIV</a></div>
<div class="p"><a href="/item/3">תיק גב לנייד Lenovo B210</a></div>
<div class="p"><a href="/item/4">מחשב נייד Lenovo V15 i5 8GB 256GB SSD</a></div>
</body></html>"""
ITEMS = {
    "/item/1": PROSE,
    "/item/2": '<div id="desc">IdeaPad עם Intel Core i5-13420H, זיכרון 16GB, אחסון 512GB SSD</div>',
}


@pytest.fixture()
def shop():
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            hits.append(self.path.split("?")[0])
            body = SEARCH if self.path.startswith("/search") else ITEMS.get(self.path, "<p>-</p>")
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}", hits
    httpd.shutdown()


def test_deep_search_end_to_end(tmp_path, monkeypatch, shop):
    url, hits = shop
    from supsearch import config, fetch
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(fetch, "DATA_DIR", str(tmp_path / "data"))
    sites = {"settings": {"channel": ""}, "sites": [{
        "id": "v", "name": "V", "fetch": "http", "search_url": url + "/search?q={query}",
        "item": "div.p", "fields": {"title": "a", "url": "a@href"},
        "detail": {"text": "#desc"}}]}
    (tmp_path / "sites.yaml").write_text(yaml.safe_dump(sites, allow_unicode=True), encoding="utf-8")
    from supsearch.app import create_app
    with TestClient(create_app(str(tmp_path / "sites.yaml"), str(tmp_path / "none.yaml")),
                    client=("127.0.0.1", 50000)) as c:
        c.post("/api/search", json={"query": "lenovo"})
        assert not any(h.startswith("/item") for h in hits)        # off by default

        j = c.post("/api/search", json={"query": "lenovo", "deep": True, "sort": "cpu_desc"}).json()
        by_title = {p["title"]: p for p in j["items"]}
        l16 = by_title["מחשב נייד Lenovo ThinkPad L16 21X8002RIV"]
        assert l16["specs"]["cpu"] == "Core Ultra 7" and l16["specs"]["ram"] == "32GB"
        assert set(l16["deep_specs"]) == {"cpu", "ram", "storage", "screen", "os"}
        assert by_title["מחשב נייד Lenovo IdeaPad Slim 3 83US005QIV"]["specs"]["cpu"] == "Core i5"
        # only computers missing specs were opened: not the bag, not the V15
        assert sorted(h for h in hits if h.startswith("/item")) == ["/item/1", "/item/2"]
        assert j["sites"][0]["deep_read"] == 2 and j["sites"][0]["deep_filled"] == 2
        # sorting by CPU now uses the specs read from the pages
        assert j["items"][0]["title"].startswith("מחשב נייד Lenovo ThinkPad L16")
        assert "cpu" in {f["key"] for f in j["facets"]}

        hits.clear()
        j = c.post("/api/search", json={"query": "lenovo", "deep": True}).json()
        assert not any(h.startswith("/item") for h in hits)        # pages are cached
        assert j["sites"][0]["deep_filled"] == 2
