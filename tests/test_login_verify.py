"""A login the site confirms by an emailed link (Wordfence on Usys), in a real browser."""

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from supsearch.fetch import VERIFY_MESSAGE, BrowserPool, NeedsLogin

LOGIN = """<html><body>{error}<form method="post" action="/login">
<input id="username" name="u"><input id="password" name="p" type="password">
<input type="hidden" name="token" value="{token}"><input type="checkbox" id="rememberme" name="r">
<button name="login" type="submit">כניסה</button></form></body></html>"""
ERROR = ('<ul class="woocommerce-error"><li><strong>VERIFICATION REQUIRED</strong>: Additional verification '
         'is required for login. Please check the email address for a verification link.</li></ul>')
RESULTS = """<html><body class="{cls}"><div class="p"><a href="/x">Lenovo V14</a>
<span class="price">{price}</span></div></body></html>"""


@pytest.fixture()
def shop():
    emails, remembered = [], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body, status=200, headers=None):
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path.startswith("/login"):
                token = parse_qs(urlsplit(self.path).query).get("wfls-email-verification", [""])[0]
                return self._send(LOGIN.format(error="", token=token))
            dealer = "dealer=1" in (self.headers.get("Cookie") or "")
            self._send(RESULTS.format(cls="logged-in" if dealer else "", price="1,234" if dealer else ""))

        def do_POST(self):
            form = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
            if form.get("u") != ["dealer"] or form.get("p") != ["pw"]:
                return self._send(LOGIN.format(error="", token=""))
            if form.get("token") != ["tok"]:      # scored as a robot: email a link instead
                emails.append(1)
                return self._send(LOGIN.format(error=ERROR, token=""))
            remembered.append(form.get("r") == ["on"])
            self._send("", 302, {"Location": "/", "Set-Cookie": "dealer=1; Path=/"})

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}", emails, remembered
    httpd.shutdown()


def test_login_finished_by_the_emailed_link(tmp_path, monkeypatch, shop):
    from supsearch import fetch
    monkeypatch.setattr(fetch, "DATA_DIR", str(tmp_path))
    url, emails, remembered = shop
    site = {"id": "u", "name": "U", "fetch": "browser", "format": "html", "item": "div.p",
            "login": {"optional": True, "url": url + "/login", "username": "#username", "password": "#password",
                      "submit": "button[name=login]", "remember": "#rememberme", "logged_in": "body.logged-in",
                      "error": ".woocommerce-error", "verify_re": "verification required"}}
    creds = {"username": "dealer", "password": "pw"}

    async def go():
        pool = BrowserPool({"channel": ""})
        try:
            with pytest.raises(NeedsLogin) as e:
                await pool.get(site, url + "/search", 15, creds)
            assert str(e.value) == VERIFY_MESSAGE and "Lenovo" in e.value.content  # guest view kept
            assert emails == [1] and pool.needs_verification("u")
            with pytest.raises(NeedsLogin):            # the next search doesn't send another email
                await pool.get(site, url + "/search", 15, creds)
            assert emails == [1]

            ok, msg = await pool.check_login(site, creds, url + "/search", 15)
            assert not ok and msg == VERIFY_MESSAGE and emails == [1, 1]

            ok, msg = await pool.verify_link(site, creds, url + "/login?wfls-email-verification=tok",
                                             url + "/search", 15)
            assert ok and msg == "ההתחברות הצליחה" and remembered == [True]
            assert not pool.needs_verification("u")
            html = await pool.get(site, url + "/search", 15, creds)
            assert "1,234" in html

            ok, msg = await pool.verify_link(site, {"username": "dealer", "password": "bad"},
                                             url + "/login?wfls-email-verification=tok", url + "/search", 15)
            assert not ok
        finally:
            await pool.close()
    asyncio.run(go())


def test_verify_link_must_be_the_suppliers(tmp_path, monkeypatch):
    import yaml
    from fastapi.testclient import TestClient
    from supsearch import app as appmod, config, fetch
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(fetch, "DATA_DIR", str(tmp_path / "data"))
    for k in ("APP_PASSWORD", "ADMIN_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    sites = {"settings": {"channel": ""}, "index": {"queries": []},
             "sites": [{"id": "u", "name": "יוניטק", "fetch": "browser", "home": "https://www.usys.co.il/",
                        "search_url": "https://www.usys.co.il/?s={query}", "item": "div.p",
                        "fields": {"title": "a"},
                        "login": {"url": "https://www.usys.co.il/my-account/", "username": "#u",
                                  "password": "#p"}}]}
    (tmp_path / "sites.yaml").write_text(yaml.safe_dump(sites, allow_unicode=True), encoding="utf-8")
    with TestClient(appmod.create_app(str(tmp_path / "sites.yaml"), str(tmp_path / "none.yaml")),
                    client=("127.0.0.1", 50000)) as c:
        assert c.post("/api/admin/suppliers/u/verify", json={"link": "https://x.co"}).status_code == 400  # no login
        c.put("/api/admin/suppliers/u", json={"username": "dealer", "password": "pw"})
        assert c.get("/api/admin/suppliers").json()[0]["needs_verification"] is False
        for bad in ("https://evil.example/my-account/?wfls-email-verification=t", "file:///etc/passwd",
                    "javascript:alert(1)", "https://usys.co.il.evil.example/"):
            r = c.post("/api/admin/suppliers/u/verify", json={"link": bad})
            assert r.status_code == 400 and "יוניטק" in r.json()["detail"], bad
