"""Price quotes (PDF) and per-user favourite suppliers."""

import io

import pytest
import yaml
from fastapi.testclient import TestClient
from pypdf import PdfReader

from supsearch import quote

BIZ = {"business_name": "בי.אס.די מחשבים", "business_id": "512345678", "phone": "03-1234567",
       "email": "a@b.co.il", "address": "תל אביב", "logo": "", "footer": "תנאי תשלום: שוטף+30",
       "markup_percent": 15, "valid_days": 14}


def test_totals_and_html():
    items = [{"title": "Lenovo <b>V15</b>", "mpn": "82YU0044IV", "image": "", "qty": 2, "unit_price": 1000}]
    assert quote.totals(items, 0.18) == (2000.0, 360.0, 2360.0)
    page = quote.quote_html(BIZ, 1001, items, customer="לקוח בע\"מ", notes="כולל התקנה", vat_rate=0.18)
    assert "הצעת מחיר מס' 1001" in page and "₪2,360.00" in page and "82YU0044IV" in page
    assert "<b>V15</b>" not in page and "&lt;b&gt;V15&lt;/b&gt;" in page   # all text escaped
    assert "שוטף+30" in page and "כולל התקנה" in page


def make_app(tmp_path, monkeypatch, env=None):
    from supsearch import app as appmod, config, fetch
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(fetch, "DATA_DIR", str(tmp_path / "data"))
    for k in ("ADMIN_PASSWORD", "APP_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    sites = {"settings": {"channel": ""}, "sites": [
        {"id": "a", "name": "א", "enabled": False}, {"id": "b", "name": "ב", "enabled": False}]}
    (tmp_path / "sites.yaml").write_text(yaml.safe_dump(sites, allow_unicode=True), encoding="utf-8")
    return TestClient(appmod.create_app(str(tmp_path / "sites.yaml"), str(tmp_path / "none.yaml")),
                      client=("127.0.0.1", 50000))


def login(c, u, p):
    assert c.post("/api/login", json={"username": u, "password": p}).status_code == 200


ITEM = {"title": "מחשב נייד Lenovo V15 i5 16GB 512GB", "mpn": "82YU0044IV", "qty": 2, "unit_price": 1000}


def test_quote_pdf(tmp_path, monkeypatch):
    with make_app(tmp_path, monkeypatch, env={"APP_PASSWORD": "admin-pass"}) as c:
        login(c, "admin", "admin-pass")
        assert c.put("/api/admin/quote-settings", json=BIZ).status_code == 200
        assert c.put("/api/admin/quote-settings", json={**BIZ, "logo": "javascript:x"}).status_code == 400
        c.post("/api/admin/users", json={"username": "dana", "password": "dana-pass"})
        c.post("/api/logout")
        login(c, "dana", "dana-pass")
        assert c.put("/api/admin/quote-settings", json=BIZ).status_code == 403   # users can't edit
        assert c.get("/api/quote/settings").json()["markup_percent"] == 15

        r = c.post("/api/quote/pdf", json={"customer": "לקוח לדוגמה", "notes": "כולל התקנה",
                                           "items": [{**ITEM, "image": "file:///etc/passwd"}]})
        assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
        assert r.headers["content-disposition"] == 'attachment; filename="quote-1001.pdf"'
        assert r.content.startswith(b"%PDF")
        text = "".join(p.extract_text() for p in PdfReader(io.BytesIO(r.content)).pages)
        for expected in ("1001", "2,360.00", "2,000.00", "360.00", "82YU0044IV", "512345678"):
            assert expected in text, expected
        assert "Lenovo" in text

        r = c.post("/api/quote/pdf", json={"items": [ITEM]})
        assert r.headers["content-disposition"].endswith('quote-1002.pdf"')        # numbers run on
        assert c.post("/api/quote/pdf", json={"items": [{**ITEM, "qty": 0}]}).status_code == 422
        assert c.post("/api/quote/pdf", json={"items": []}).status_code == 422


def test_quote_settings_reject_big_logo(tmp_path):
    from supsearch.store import Store, StoreError
    st = Store(str(tmp_path))
    with pytest.raises(StoreError):
        st.set_quote_settings({"logo": "data:image/png;base64," + "A" * 500_000})
    st.set_quote_settings({"business_name": "x", "unknown_key": "ignored"})
    assert st.quote_settings()["business_name"] == "x" and "unknown_key" not in st.quote_settings()


def test_favorites_per_user(tmp_path, monkeypatch):
    with make_app(tmp_path, monkeypatch) as c:
        # open mode (no users yet): nothing to save favourites on
        assert c.put("/api/me/favorites", json={"sites": ["a"]}).status_code == 400
        c.post("/api/admin/users", json={"username": "boss", "password": "123456"})
        login(c, "boss", "123456")
        c.post("/api/admin/users", json={"username": "dana", "password": "dana-pass"})
        r = c.put("/api/me/favorites", json={"sites": ["b", "nope", "a", "b"]})
        assert r.json()["favorites"] == ["b", "a"]                                 # known, unique, in order
        assert c.get("/api/me").json()["favorites"] == ["b", "a"]
        c.post("/api/logout")
        login(c, "dana", "dana-pass")
        assert c.get("/api/me").json()["favorites"] == []                         # each user their own


def test_totals_with_vat_included():
    items = [{"title": "x", "qty": 2, "unit_price": 1180}]
    assert quote.totals(items, 0.18, inclusive=True) == (2000.0, 360.0, 2360.0)  # total is what was typed
    assert quote.totals([{"title": "x", "qty": 3, "unit_price": 99.9}], 0.18, inclusive=True)[2] == 299.7


def test_pdf_with_vat_included_and_specs(tmp_path, monkeypatch):
    with make_app(tmp_path, monkeypatch) as c:
        specs = [{"label": "מעבד", "value": "Intel Core Ultra 7 355"}, {"label": "זיכרון", "value": "32GB DDR5-5600"}]
        r = c.post("/api/quote/pdf", json={"prices_include_vat": True, "items": [{
            "title": "Lenovo ThinkPad L16", "qty": 1, "unit_price": 5900, "specs": specs,
            "spec_source": "אתר היצרן · https://psref.lenovo.com/x"}]})
        assert r.status_code == 200
        text = "".join(p.extract_text() for p in PdfReader(io.BytesIO(r.content)).pages)
        for expected in ("5,900.00", "5,000.00", "900.00", "Intel Core Ultra 7 355", "32GB DDR5",
                         "psref.lenovo.com"):
            assert expected in text, expected


# -- the full spec sheet ---------------------------------------------------------------

from types import SimpleNamespace as NS  # noqa: E402

from supsearch import ai  # noqa: E402

SPEC_PAGE = """<ul id="spec"><li><span>דגם מעבד</span><span>Intel Core Ultra 5 225T</span></li>
<li><span>זכרון</span><span>16GB</span></li><li><span>אחסון</span><span>512GB SSD</span></li></ul>"""


class FakeSpecs:
    def __init__(self, found=True):
        self.calls = []
        self.found = found
        self.beta = NS(messages=NS(create=self.create))

    async def create(self, **kw):
        self.calls.append(kw)
        usage = NS(input_tokens=5000, output_tokens=800, cache_read_input_tokens=0,
                   cache_creation_input_tokens=0, server_tool_use=NS(web_search_requests=2))
        if len(self.calls) == 1:  # the server-side search loop paused: resumed by re-sending
            return NS(stop_reason="pause_turn", usage=usage, content=[NS(type="server_tool_use")])
        return NS(stop_reason="tool_use", usage=usage, content=[NS(
            type="tool_use", name="submit_specs", id="t1", input={
                "found": self.found, "model_name": "ThinkPad L16 Gen 3", "source_url": "https://psref.lenovo.com/x",
                "specs": [{"label": "מעבד", "value": "Intel Core Ultra 7 355"},
                          {"label": "מסך", "value": '16" WUXGA IPS'}] if self.found else []})])


@pytest.fixture()
def spec_shop():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = SPEC_PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def specs_app(tmp_path, monkeypatch, shop_url, env=None):
    from supsearch import app as appmod, config, fetch
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(fetch, "DATA_DIR", str(tmp_path / "data"))
    for k in ("ADMIN_PASSWORD", "APP_PASSWORD", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    sites = {"settings": {"channel": ""}, "sites": [
        {"id": "t", "name": "טכנו", "fetch": "http", "search_url": shop_url + "/s?q={query}", "item": "x",
         "detail": {"rows": "#spec li"}}]}
    (tmp_path / "sites.yaml").write_text(yaml.safe_dump(sites, allow_unicode=True), encoding="utf-8")
    return TestClient(appmod.create_app(str(tmp_path / "sites.yaml"), str(tmp_path / "none.yaml")),
                      client=("127.0.0.1", 50000))


REQ = {"title": "Lenovo ThinkPad L16", "mpn": "21X8-002RIV", "brand": "Lenovo", "site": "t"}


def test_specs_from_the_manufacturer(tmp_path, monkeypatch, spec_shop):
    fake = FakeSpecs()
    monkeypatch.setattr(ai, "make_client", lambda key: fake)
    with specs_app(tmp_path, monkeypatch, spec_shop, env={"ANTHROPIC_API_KEY": "k"}) as c:
        j = c.post("/api/quote/specs", json={**REQ, "url": spec_shop + "/p/1"}).json()
        assert j["source"] == "אתר היצרן" and j["source_url"] == "https://psref.lenovo.com/x"
        assert j["specs"][0] == {"label": "מעבד", "value": "Intel Core Ultra 7 355"} and not j["cached"]
        assert j["cost_usd"] > 0
        first = fake.calls[0]
        web = {t["name"]: t for t in first["tools"]}
        assert web["web_search"]["type"] == "web_search_20260209"
        assert "lenovo.com" in web["web_search"]["allowed_domains"]       # official sites only
        assert web["submit_specs"]["strict"] and first["fallbacks"] == "default"
        assert len(fake.calls) == 2 and fake.calls[1]["messages"][-1]["role"] == "assistant"  # resumed
        # the same part number again (any spelling): from the cache, no new lookup or cost
        j = c.post("/api/quote/specs", json={**REQ, "mpn": "21X8002RIV"}).json()
        assert j["cached"] and j["cost_usd"] == 0 and len(fake.calls) == 2


def test_specs_fall_back_to_the_supplier_page(tmp_path, monkeypatch, spec_shop):
    monkeypatch.setattr(ai, "make_client", lambda key: FakeSpecs(found=False))
    with specs_app(tmp_path, monkeypatch, spec_shop, env={"ANTHROPIC_API_KEY": "k"}) as c:
        j = c.post("/api/quote/specs", json={**REQ, "url": spec_shop + "/p/1"}).json()
        assert j["source"] == "דף המוצר אצל טכנו" and len(j["specs"]) == 3
        assert j["specs"][0] == {"label": "דגם מעבד", "value": "Intel Core Ultra 5 225T"}
        assert "לא נמצא" in j["ai_note"]


def test_specs_without_key_or_page(tmp_path, monkeypatch, spec_shop):
    with specs_app(tmp_path, monkeypatch, spec_shop) as c:
        j = c.post("/api/quote/specs", json={**REQ, "url": spec_shop + "/p/1"}).json()
        assert j["source"] == "דף המוצר אצל טכנו"                       # no key: the supplier's table
        r = c.post("/api/quote/specs", json={**REQ, "mpn": "OTHER1", "site": "", "url": ""})
        assert r.status_code == 404 and "מפתח API" in r.json()["detail"]



def test_quote_never_names_the_supplier(tmp_path, monkeypatch, spec_shop):
    with specs_app(tmp_path, monkeypatch, spec_shop) as c:
        host = spec_shop.split("//")[1].split(":")[0]  # the supplier's host (127.0.0.1 here)
        r = c.post("/api/quote/pdf", json={"items": [{"title": "PC", "qty": 1, "unit_price": 100,
            "specs": [{"label": "מעבד", "value": "i5"}],
            "spec_source": f"דף המוצר אצל טכנו · http://{host}/p/1"}]})
        text = "".join(p.extract_text() for p in PdfReader(io.BytesIO(r.content)).pages)
        assert "i5" in text and host not in text and "טכנו" not in text
