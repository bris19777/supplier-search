"""Loading config/sites.yaml and config/credentials.yaml.

sites.yaml describes every supplier site declaratively (search URL, how to
fetch it, CSS selectors / JSON paths for each field), so a site can be added or
fixed without touching Python. credentials.yaml holds dealer logins and is
never committed.
"""

import os

import yaml

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.environ.get("SUPSEARCH_CONFIG", os.path.join(BASE, "config"))
DATA_DIR = os.environ.get("SUPSEARCH_DATA", os.path.join(BASE, "data"))

DEFAULT_SETTINGS = {
    "headless": True,
    "channel": "chrome",       # use the installed Google Chrome; falls back to Playwright's Chromium
    "timeout": 30,             # seconds per site per search
    "cache_minutes": 10,
    "vat_rate": 0.18,
    "proxy": "",               # send all supplier traffic through this proxy, see OUTBOUND_PROXY
}


def load_sites(path=None):
    path = path or os.path.join(CONFIG_DIR, "sites.yaml")
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    settings = {**DEFAULT_SETTINGS, **(raw.get("settings") or {})}
    settings["index"] = raw.get("index") or {}  # local index / background crawl, see index.py
    if os.environ.get("INDEX_CRAWL") == "0":
        settings["index"]["enabled"] = False
    # On Fly the container's start script points this at Tailscale's local proxy,
    # so the suppliers see the office IP (the office PC is the Tailscale exit node)
    if os.environ.get("OUTBOUND_PROXY"):
        settings["proxy"] = os.environ["OUTBOUND_PROXY"]
    sites = []
    for s in raw.get("sites") or []:
        if not s.get("id"):
            raise ValueError(f"site entry needs an id: {s!r}")
        s.setdefault("name", s["id"])
        s.setdefault("enabled", True)
        if s["enabled"] and not (s.get("search_url") and s.get("item")):
            raise ValueError(f"enabled site {s['id']} needs search_url and item")
        s.setdefault("fetch", "http")
        s.setdefault("format", "html")
        s.setdefault("pages", 1)
        s.setdefault("vat_included", True)
        s.setdefault("fields", {})
        sites.append(s)
    ids = [s["id"] for s in sites]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate site id in sites.yaml")
    return settings, sites


def load_credentials(path=None):
    """{site_id: {"username": ..., "password": ...}}; missing file means no logins.

    On a cloud host the same YAML comes from the SUPPLIER_CREDENTIALS secret, and
    its entries take precedence over the file."""
    path = path or os.path.join(CONFIG_DIR, "credentials.yaml")
    creds = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            creds = yaml.safe_load(f) or {}
    env = os.environ.get("SUPPLIER_CREDENTIALS", "").strip()
    if env:
        creds.update(yaml.safe_load(env) or {})
    return creds
