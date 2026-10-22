"""Dealer login through a form with an arithmetic captcha (as on Usys), in a real browser."""

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest

from supsearch.fetch import BrowserPool, solve_sum

LOGIN = """<html><body><form method="post" action="/login">
<input id="username" name="u"><input id="password" name="p" type="password">
<label>כמה זה <span id="q">8 + 2</span>?</label><input id="ans" name="a" required>
<button name="login" type="submit">כניסה</button></form></body></html>"""
RESULTS = """<html><body class="{cls}"><div class="p"><a href="/x">Lenovo V14</a>
<span class="price">{price}</span></div></body></html>"""


@pytest.fixture()
def shop():
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
                return self._send(LOGIN)
            dealer = "dealer=1" in (self.headers.get("Cookie") or "")
            self._send(RESULTS.format(cls="logged-in" if dealer else "", price="1,234" if dealer else ""))

        def do_POST(self):
            form = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
            if form.get("u") == ["dealer"] and form.get("p") == ["pw"] and form.get("a") == ["10"]:
                return self._send("", 302, {"Location": "/", "Set-Cookie": "dealer=1; Path=/"})
            self._send(LOGIN)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def test_solve_sum():
    assert solve_sum("8 + 2") == 10 and solve_sum("12 - 5 = ?") == 7 and solve_sum("3 × 4") == 12
    assert solve_sum("no question") is None


def test_login_with_captcha(tmp_path, monkeypatch, shop):
    from supsearch import fetch
    monkeypatch.setattr(fetch, "DATA_DIR", str(tmp_path))
    site = {"id": "u", "name": "U", "fetch": "browser", "format": "html", "item": "div.p",
            "login": {"optional": True, "url": shop + "/login", "username": "#username", "password": "#password",
                      "submit": "button[name=login]", "logged_in": "body.logged-in",
                      "captcha": {"question": "#q", "answer": "#ans"}}}

    async def go():
        pool = BrowserPool({"channel": ""})
        try:
            html = await pool.get(site, shop + "/search", 15, {"username": "dealer", "password": "pw"})
            assert "1,234" in html and "logged-in" in html
        finally:
            await pool.close()
    asyncio.run(go())
