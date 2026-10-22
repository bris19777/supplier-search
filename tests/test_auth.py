"""User accounts, sessions, roles and the shared (encrypted) supplier logins."""

import json
import time

import pytest
import yaml
from fastapi.testclient import TestClient

from supsearch.store import Credentials, Store, StoreError, verify_password, hash_password
from test_e2e import server  # noqa: F401  (mock supplier sites fixture)


# -- store ---------------------------------------------------------------------------

def test_password_hashing():
    h = hash_password("secret1")
    assert verify_password("secret1", h) and not verify_password("secret2", h)
    assert hash_password("secret1") != h  # salted
    assert not verify_password("x", "garbage")


def test_users_first_is_admin_and_last_admin_protected(tmp_path):
    st = Store(str(tmp_path))
    assert not st.has_users()
    assert st.create_user("Boss", "123456", "user")["role"] == "admin"  # first user
    st.create_user("dana", "123456")
    assert st.get_user("boss") == {"username": "boss", "role": "admin"}  # names are lower-cased
    with pytest.raises(StoreError):
        st.create_user("dana", "abcdef")
    with pytest.raises(StoreError):
        st.create_user("x y", "abcdef")
    with pytest.raises(StoreError):
        st.create_user("short", "123")
    with pytest.raises(StoreError):
        st.update_user("boss", role="user")
    with pytest.raises(StoreError):
        st.delete_user("boss")
    st.update_user("dana", role="admin")
    st.update_user("boss", role="user")  # fine now: dana is an admin
    assert st.authenticate("DANA", "123456")["role"] == "admin"
    assert st.authenticate("dana", "wrong") is None


def test_sessions(tmp_path):
    st = Store(str(tmp_path))
    st.create_user("boss", "123456")
    token = st.new_session("boss")
    assert st.session_user(token)["username"] == "boss"
    assert token not in (tmp_path / "sessions.json").read_text()  # only a hash is stored
    st.update_user("boss", password="newpass1")  # a password reset ends old sessions
    assert st.session_user(token) is None
    token = st.new_session("boss")
    st.end_session(token)
    assert st.session_user(token) is None
    st.create_user("dana", "123456")
    token = st.new_session("dana")
    st.delete_user("dana")
    assert st.session_user(token) is None


def test_supplier_logins_encrypted(tmp_path):
    st = Store(str(tmp_path), secret_key="k1")
    st.set_supplier_login("techno", "me@shop.co.il", "Sup3rSecret", by="boss")
    raw = (tmp_path / "suppliers.json").read_text()
    assert "Sup3rSecret" not in raw and "me@shop.co.il" in raw
    assert st.supplier_login("techno") == {"username": "me@shop.co.il", "password": "Sup3rSecret"}
    st.set_supplier_login("techno", "other@shop.co.il", "", by="boss")  # keep password
    assert st.supplier_login("techno")["password"] == "Sup3rSecret"
    status = st.supplier_status()["techno"]
    assert status["ok"] and status["updated_by"] == "boss" and "password" not in status
    # another key cannot read it: the admin is asked to enter it again
    other = Store(str(tmp_path), secret_key="k2")
    assert other.supplier_login("techno") is None and other.supplier_status()["techno"]["ok"] is False
    with pytest.raises(StoreError):
        st.set_supplier_login("cms", "u", "", by="boss")  # new site needs a password


def test_credentials_prefer_saved_over_config(tmp_path):
    st = Store(str(tmp_path))
    creds = Credentials(st, {"a": {"username": "file", "password": "x"}})
    assert creds.get("a")["username"] == "file" and "a" in creds and "b" not in creds
    st.set_supplier_login("a", "saved", "y", by="boss")
    assert creds.get("a") == {"username": "saved", "password": "y"}


# -- API -----------------------------------------------------------------------------

def make_app(tmp_path, monkeypatch, server_url=None, env=None, client_host="127.0.0.1"):
    from supsearch import app as appmod, config, fetch
    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", str(data))
    monkeypatch.setattr(fetch, "DATA_DIR", str(data))
    for k in ("ADMIN_PASSWORD", "APP_PASSWORD", "ADMIN_USERNAME", "SUPPLIER_CREDENTIALS"):
        monkeypatch.delenv(k, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(appmod, "REQUIRE_PASSWORD", bool((env or {}).get("REQUIRE_PASSWORD")))
    sites = {"settings": {"channel": "", "timeout": 20}, "sites": [
        {"id": "b", "name": "חנות ב", "fetch": "browser", "vat_included": False,
         "search_url": f"{server_url}/b/search?q={{query}}", "item": "li.row",
         "login": {"optional": True, "url": f"{server_url}/b/login", "username": "#user",
                   "password": "#pass", "submit": "#go", "logged_in": "a.logout"},
         "fields": {"title": "h4 a", "price": ".pr"}},
        {"id": "cms", "name": "CMS", "enabled": False, "fetch": "browser"},
    ]} if server_url else {"sites": [{"id": "cms", "name": "CMS", "enabled": False}]}
    (tmp_path / "sites.yaml").write_text(yaml.safe_dump(sites, allow_unicode=True), encoding="utf-8")
    return TestClient(appmod.create_app(str(tmp_path / "sites.yaml"), str(tmp_path / "none.yaml")),
                      client=(client_host, 50000))


def login(c, user, password):
    return c.post("/api/login", json={"username": user, "password": password})


def test_open_mode_until_first_admin(tmp_path, monkeypatch):
    with make_app(tmp_path, monkeypatch) as c:
        assert c.get("/api/me").json() == {"username": "", "role": "admin", "open": True, "favorites": []}
        r = c.post("/api/admin/users", json={"username": "boss", "password": "123456", "role": "user"})
        assert r.json()["role"] == "admin" and r.json()["login_required"] is True
        assert c.get("/api/sites").status_code == 401
        assert c.get("/", follow_redirects=False).status_code == 303   # page -> login
        assert login(c, "boss", "nope").status_code == 401
        assert login(c, "boss", "123456").status_code == 200
        assert c.get("/api/me").json()["username"] == "boss"
        assert c.get("/").status_code == 200 and c.get("/admin").status_code == 200
        c.post("/api/logout")
        assert c.get("/api/me").status_code == 401


def test_open_mode_only_from_the_server_itself(tmp_path, monkeypatch):
    with make_app(tmp_path, monkeypatch, client_host="192.168.1.20") as c:  # another LAN machine
        assert c.get("/api/me").status_code == 503
        assert c.post("/api/admin/users", json={"username": "x", "password": "123456"}).status_code == 503
    with make_app(tmp_path, monkeypatch) as c:
        # a proxied request is not local even if it arrives on 127.0.0.1
        assert c.get("/api/me", headers={"x-forwarded-for": "8.8.8.8"}).status_code == 503


def test_locked_on_cloud_without_admin(tmp_path, monkeypatch):
    with make_app(tmp_path, monkeypatch, env={"REQUIRE_PASSWORD": "1"}) as c:
        assert c.get("/api/sites").status_code == 503
        assert c.get("/healthz").status_code == 200


def test_first_admin_from_env_and_roles(tmp_path, monkeypatch):
    env = {"REQUIRE_PASSWORD": "1", "APP_PASSWORD": "admin-pass"}
    with make_app(tmp_path, monkeypatch, env=env) as c:
        assert login(c, "admin", "admin-pass").status_code == 200
        assert c.post("/api/admin/users", json={"username": "dana", "password": "dana-pass"}).status_code == 200
        assert c.delete("/api/admin/users/admin").status_code == 400   # not yourself
        c.post("/api/logout")
        assert login(c, "dana", "dana-pass").json()["role"] == "user"
        assert c.get("/api/sites").status_code == 200                    # users can search
        assert c.get("/api/admin/users").status_code == 403              # but not manage
        assert c.get("/api/admin/suppliers").status_code == 403
        assert c.put("/api/admin/suppliers/cms", json={"username": "u", "password": "p"}).status_code == 403
        assert c.get("/admin", follow_redirects=False).status_code == 303
        r = c.post("/api/me/password", json={"current": "dana-pass", "new": "dana-pass2"})
        assert r.status_code == 200
        assert c.get("/api/me").status_code == 401                       # logged out by the change
        assert login(c, "dana", "dana-pass2").status_code == 200


def test_login_lockout(tmp_path, monkeypatch):
    with make_app(tmp_path, monkeypatch, env={"APP_PASSWORD": "admin-pass"}) as c:
        for _ in range(5):
            assert login(c, "admin", "bad").status_code == 401
        assert login(c, "admin", "admin-pass").status_code == 429


def test_supplier_password_never_returned(tmp_path, monkeypatch):
    with make_app(tmp_path, monkeypatch, env={"APP_PASSWORD": "admin-pass"}) as c:
        login(c, "admin", "admin-pass")
        assert c.put("/api/admin/suppliers/cms", json={"username": "me", "password": "TopSecret9"}).status_code == 200
        assert c.put("/api/admin/suppliers/nope", json={"username": "me", "password": "x"}).status_code == 404
        body = c.get("/api/admin/suppliers").text
        assert "TopSecret9" not in body
        row = next(s for s in json.loads(body) if s["id"] == "cms")
        assert row["saved"]["username"] == "me" and row["saved"]["updated_by"] == "admin"
        assert c.delete("/api/admin/suppliers/cms").status_code == 200
        assert next(s for s in c.get("/api/admin/suppliers").json() if s["id"] == "cms")["saved"] is None


def test_saved_supplier_login_used_by_search(tmp_path, monkeypatch, server):  # noqa: F811
    with make_app(tmp_path, monkeypatch, server_url=server, env={"APP_PASSWORD": "admin-pass"}) as c:
        login(c, "admin", "admin-pass")
        # no login saved: guest results, no dealer price
        j = c.post("/api/search", json={"query": "ssd"}).json()
        assert j["sites"][0]["needs_login"] and j["items"][0]["price"] is None
        # a wrong password fails the check
        c.put("/api/admin/suppliers/b", json={"username": "dealer", "password": "wrong"})
        r = c.post("/api/admin/suppliers/b/test").json()
        assert r["ok"] is False
        # the right one: the check passes and every user's search gets dealer prices
        c.put("/api/admin/suppliers/b", json={"username": "dealer", "password": "s3cret"})
        r = c.post("/api/admin/suppliers/b/test").json()
        assert r == {"ok": True, "message": "ההתחברות הצליחה"}
        c.post("/api/admin/users", json={"username": "dana", "password": "dana-pass"})
        c.post("/api/logout")
        login(c, "dana", "dana-pass")
        j = c.post("/api/search", json={"query": "ssd", "refresh": True}).json()
        assert not j["sites"][0]["needs_login"], j["sites"]
        assert j["items"][0]["price"] == 212.0
