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
