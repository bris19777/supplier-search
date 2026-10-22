"""Users, sessions and the shared supplier logins, kept as JSON files in DATA_DIR.

- users.json      username -> scrypt hash, role ("admin" | "user")
- sessions.json   sha256(session token) -> username, expiry (raw tokens live only in cookies)
- suppliers.json  site id -> username + password, the password encrypted with SECRET_KEY

The supplier passwords are shared by every user of the system but are only ever
written by an admin and never sent back to the browser; the server reads them
to log in to the supplier sites.
"""

import base64
import hashlib
import json
import os
import secrets
import threading
import time

from cryptography.fernet import Fernet, InvalidToken

SESSION_DAYS = 30
ROLES = ("admin", "user")
MIN_PASSWORD = 6


class StoreError(ValueError):
    """A request the store refuses, with a message fit for the user (Hebrew)."""


def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password, stored):
    try:
        salt_b64, _ = stored.split("$", 1)
        expected = hash_password(password, base64.b64decode(salt_b64))
    except (ValueError, TypeError):
        return False
    return secrets.compare_digest(expected, stored)


def _fernet(data_dir, secret_key):
    """SECRET_KEY (any string) -> cipher. Without one, a random key file next to the data,
    which is fine on the office PC; on Fly the deploy sets SECRET_KEY so the volume alone
    doesn't reveal the supplier passwords."""
    if secret_key:
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret_key.encode()).digest()))
    path = os.path.join(data_dir, "secret.key")
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(Fernet.generate_key())
        os.chmod(path, 0o600)
    with open(path, "rb") as f:
        return Fernet(f.read().strip())


def clean_username(name):
    name = (name or "").strip().lower()
    if not name or len(name) > 40 or any(c.isspace() for c in name):
        raise StoreError("שם משתמש לא תקין (עד 40 תווים, בלי רווחים)")
    return name


class Store:
    def __init__(self, data_dir, secret_key=None):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self._lock = threading.Lock()
        self._fernet = _fernet(data_dir, secret_key)

    # -- files ---------------------------------------------------------------

    def _path(self, name):
        return os.path.join(self.data_dir, name)

    def _read(self, name):
        try:
            with open(self._path(name), encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def _write(self, name, data):
        tmp = self._path(name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._path(name))  # atomic: never a half-written file

    # -- users ---------------------------------------------------------------

    def has_users(self):
        return bool(self._read("users.json"))

    def list_users(self):
        users = self._read("users.json")
        return [{"username": u, "role": v["role"], "created": v.get("created")}
                for u, v in sorted(users.items())]

    def get_user(self, username):
        u = self._read("users.json").get(username)
        return {"username": username, "role": u["role"]} if u else None

    def create_user(self, username, password, role="user"):
        username = clean_username(username)
        if role not in ROLES:
            raise StoreError("תפקיד לא תקין")
        if len(password or "") < MIN_PASSWORD:
            raise StoreError(f"סיסמה קצרה מדי (לפחות {MIN_PASSWORD} תווים)")
        with self._lock:
            users = self._read("users.json")
            if username in users:
                raise StoreError("שם המשתמש כבר קיים")
            if not users:
                role = "admin"  # the first user always administers the system
            users[username] = {"hash": hash_password(password), "role": role,
                               "created": time.strftime("%Y-%m-%d")}
            self._write("users.json", users)
        return {"username": username, "role": role}

    def update_user(self, username, password=None, role=None):
        with self._lock:
            users = self._read("users.json")
            if username not in users:
                raise StoreError("משתמש לא קיים")
            if role is not None:
                if role not in ROLES:
                    raise StoreError("תפקיד לא תקין")
                if role != "admin" and self._admins(users) == [username]:
                    raise StoreError("חייב להישאר לפחות מנהל אחד")
                users[username]["role"] = role
            if password is not None:
                if len(password) < MIN_PASSWORD:
                    raise StoreError(f"סיסמה קצרה מדי (לפחות {MIN_PASSWORD} תווים)")
                users[username]["hash"] = hash_password(password)
            self._write("users.json", users)
        if password is not None:
            self.end_sessions(username)  # a reset password logs out the old sessions

    def delete_user(self, username):
        with self._lock:
            users = self._read("users.json")
            if username not in users:
                raise StoreError("משתמש לא קיים")
            if self._admins(users) == [username]:
                raise StoreError("אי אפשר למחוק את המנהל האחרון")
            del users[username]
            self._write("users.json", users)
        self.end_sessions(username)

    @staticmethod
    def _admins(users):
        return [u for u, v in users.items() if v["role"] == "admin"]

    def authenticate(self, username, password):
        try:
            username = clean_username(username)
        except StoreError:
            return None
        u = self._read("users.json").get(username)
        if not u or not verify_password(password or "", u["hash"]):
            return None
        return {"username": username, "role": u["role"]}

    # -- sessions --------------------------------------------------------------

    @staticmethod
    def _token_key(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def new_session(self, username):
        token = secrets.token_urlsafe(32)
        with self._lock:
            sessions = {k: v for k, v in self._read("sessions.json").items() if v["expires"] > time.time()}
            sessions[self._token_key(token)] = {"user": username,
                                                "expires": time.time() + SESSION_DAYS * 86400}
            self._write("sessions.json", sessions)
        return token

    def session_user(self, token):
        if not token:
            return None
        s = self._read("sessions.json").get(self._token_key(token))
        if not s or s["expires"] < time.time():
            return None
        return self.get_user(s["user"])  # a deleted user's session stops working at once

    def end_session(self, token):
        with self._lock:
            sessions = self._read("sessions.json")
            if sessions.pop(self._token_key(token or ""), None) is not None:
                self._write("sessions.json", sessions)

    def end_sessions(self, username):
        with self._lock:
            sessions = self._read("sessions.json")
            kept = {k: v for k, v in sessions.items() if v["user"] != username}
            if len(kept) != len(sessions):
                self._write("sessions.json", kept)

    # -- supplier logins (shared by all users) ---------------------------------

    def supplier_status(self):
        """site -> {username, updated_by, updated_at, ok}; never the password."""
        out = {}
        for site, v in self._read("suppliers.json").items():
            out[site] = {"username": v["username"], "updated_by": v.get("updated_by"),
                         "updated_at": v.get("updated_at"),
                         "ok": self.supplier_login(site) is not None}
        return out

    def supplier_login(self, site):
        v = self._read("suppliers.json").get(site)
        if not v:
            return None
        try:
            password = self._fernet.decrypt(v["password"].encode()).decode()
        except InvalidToken:
            return None  # SECRET_KEY changed: the admin has to enter it again
        return {"username": v["username"], "password": password}

    def set_supplier_login(self, site, username, password, by):
        if not (username or "").strip():
            raise StoreError("יש להזין שם משתמש")
        with self._lock:
            data = self._read("suppliers.json")
            if not password:
                # username-only edit keeps the saved password
                old = data.get(site)
                if not old:
                    raise StoreError("יש להזין סיסמה")
                password_enc = old["password"]
            else:
                password_enc = self._fernet.encrypt(password.encode()).decode()
            data[site] = {"username": username.strip(), "password": password_enc,
                          "updated_by": by, "updated_at": time.strftime("%Y-%m-%d %H:%M")}
            self._write("suppliers.json", data)

    def delete_supplier_login(self, site):
        with self._lock:
            data = self._read("suppliers.json")
            if data.pop(site, None) is not None:
                self._write("suppliers.json", data)


    # -- per-user favourite suppliers ----------------------------------------------

    def get_favorites(self, username):
        u = self._read("users.json").get(username)
        return list(u.get("favorites", [])) if u else []

    def set_favorites(self, username, sites):
        with self._lock:
            users = self._read("users.json")
            if username not in users:
                raise StoreError("משתמש לא קיים")
            users[username]["favorites"] = list(dict.fromkeys(sites))  # unique, order kept
            self._write("users.json", users)

    # -- quotes: business details on the PDF, and the running quote number ----------

    QUOTE_DEFAULTS = {"business_name": "", "business_id": "", "phone": "", "email": "", "address": "",
                      "logo": "", "footer": "", "markup_percent": 15.0, "valid_days": 14}

    def quote_settings(self):
        return {**self.QUOTE_DEFAULTS, **self._read("quote_settings.json")}

    def set_quote_settings(self, values):
        clean = {k: values[k] for k in self.QUOTE_DEFAULTS if k in values}
        if len(clean.get("logo", "")) > 400_000:
            raise StoreError("הלוגו גדול מדי (עד 300KB)")
        if clean.get("logo") and not clean["logo"].startswith("data:image/"):
            raise StoreError("הלוגו חייב להיות תמונה")
        with self._lock:
            self._write("quote_settings.json", {**self.quote_settings(), **clean})

    def next_quote(self, by, customer, total):
        """Reserve the next quote number and keep a short log of issued quotes."""
        with self._lock:
            data = self._read("quotes.json")
            number = int(data.get("next", 1001))
            log = data.get("log", [])[-499:]
            log.append({"number": number, "by": by, "customer": customer, "total": total,
                        "at": time.strftime("%Y-%m-%d %H:%M")})
            self._write("quotes.json", {"next": number + 1, "log": log})
        return number

    # -- app secrets (the Anthropic API key for AI search) ------------------------

    def set_secret(self, name, value, by):
        if not (value or "").strip():
            raise StoreError("יש להזין ערך")
        with self._lock:
            data = self._read("secrets.json")
            data[name] = {"value": self._fernet.encrypt(value.strip().encode()).decode(),
                          "hint": value.strip()[-4:], "updated_by": by,
                          "updated_at": time.strftime("%Y-%m-%d %H:%M")}
            self._write("secrets.json", data)

    def get_secret(self, name):
        v = self._read("secrets.json").get(name)
        if not v:
            return None
        try:
            return self._fernet.decrypt(v["value"].encode()).decode()
        except InvalidToken:
            return None

    def secret_status(self, name):
        """{hint (last 4 chars), updated_by, updated_at, ok} or None; never the value."""
        v = self._read("secrets.json").get(name)
        if not v:
            return None
        return {"hint": v.get("hint", ""), "updated_by": v.get("updated_by"),
                "updated_at": v.get("updated_at"), "ok": self.get_secret(name) is not None}

    def delete_secret(self, name):
        with self._lock:
            data = self._read("secrets.json")
            if data.pop(name, None) is not None:
                self._write("secrets.json", data)


class Credentials:
    """What the aggregator asks for a site's login: the admin-saved one, else the
    credentials.yaml / SUPPLIER_CREDENTIALS fallback."""

    def __init__(self, store, static=None):
        self.store = store
        self.static = static or {}

    def get(self, site_id):
        return self.store.supplier_login(site_id) or self.static.get(site_id)

    def __contains__(self, site_id):
        return self.get(site_id) is not None
