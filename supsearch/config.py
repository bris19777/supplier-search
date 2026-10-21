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
}


def load_sites(path=None):
    path = path or os.path.join(CONFIG_DIR, "sites.yaml")
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    settings = {**DEFAULT_SETTINGS, **(raw.get("settings") or {})}
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
    """{site_id: {"username": ..., "password": ...}}; missing file means no logins."""
    path = path or os.path.join(CONFIG_DIR, "credentials.yaml")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}
