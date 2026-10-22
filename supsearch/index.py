"""Local product index: answer searches in milliseconds instead of a live scrape.

Every product the system sees (in user searches, AI searches and the background
crawl) is kept in a SQLite database with an FTS5 full-text table over its title,
part numbers and brand. A search goes to the index first; only when the index
has nothing for the query (or the user asks for a full refresh) do the supplier
sites get searched live, and those results are indexed too.

The background crawl keeps the index covering the catalogs: every few hours it
runs a list of broad queries (brands, categories; `index:` in sites.yaml) at
each supplier, one request at a time per site.
"""

import asyncio
import json
import os
import re
import sqlite3
import threading
import time

from dataclasses import fields

from .models import Product, SiteResult
from .normalize import norm_mpn

DEFAULT_QUERIES = [
    "lenovo", "hp", "dell", "asus", "acer", "msi", "gigabyte", "samsung", "apple", "microsoft",
    "logitech", "kingston", "western digital", "seagate", "tp-link", "ubiquiti", "epson", "brother",
    "מחשב נייד", "מחשב נייח", "laptop", "notebook", "desktop", "מסך", "monitor", "ssd", "nvme",
    "ddr5", "ddr4", "rtx", "כרטיס מסך", "מדפסת", "printer", "מקלדת", "עכבר", "ups", "switch", "router",
]
DEFAULT_SETTINGS = {"enabled": True, "every_hours": 12, "pages": 5, "deep": False,
                    "max_age_days": 3, "prune_days": 14, "pause_seconds": 1.0}

_PRODUCT_FIELDS = {f.name for f in fields(Product)}
_TOKEN = re.compile(r"[0-9A-Za-z֐-׿]+")


def _key(p):
    return f"{p['site']}|{p['url'] or p['title']}"


def _fts_text(p):
    # the compact part numbers make "SNV3S1000G" find "SNV3S/1000G" and the reverse
    return " ".join([p["title"], p["mpn"], p["sku"], p["brand"], norm_mpn(p["mpn"]), norm_mpn(p["sku"])])


def fts_query(query):
    """Every word must match, as a prefix ("lenov" finds "Lenovo")."""
    tokens = _TOKEN.findall(query or "")
    return " ".join('"' + t.replace('"', "") + '"*' for t in tokens)


class Index:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("""CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY, key TEXT UNIQUE, site TEXT, data TEXT, last_seen REAL)""")
            self._db.execute("CREATE INDEX IF NOT EXISTS products_site ON products(site, last_seen)")
            self._db.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS products_fts USING fts5(
                text, tokenize="unicode61 remove_diacritics 2")""")
            # out-of-stock products are never kept (rows from before that rule included)
            for (i,) in self._db.execute(
                    "SELECT id FROM products WHERE json_extract(data, '$.in_stock') = 0").fetchall():
                self._db.execute("DELETE FROM products_fts WHERE rowid=?", (i,))
                self._db.execute("DELETE FROM products WHERE id=?", (i,))

    def upsert(self, products, touch=True):
        """Store product dicts. Specs read earlier from a product page (deep search)
        are kept when a later sighting only has the title's specs. touch=False keeps
        an existing row's last-seen time (saving specs is not a fresh sighting)."""
        seen_at = time.time()
        with self._lock, self._db:
            for p in products:
                if p.get("in_stock") is False:
                    self._delete_key(_key(p))  # went out of stock: forget it
                    continue
                key = _key(p)
                row = self._db.execute("SELECT id, data FROM products WHERE key=?", (key,)).fetchone()
                if row:
                    old = json.loads(row[1])
                    p = {**p, "specs": {**old.get("specs", {}), **p["specs"]},
                         "deep_specs": sorted(set(old.get("deep_specs", [])) | set(p.get("deep_specs", [])))}
                    self._db.execute("UPDATE products SET data=?, last_seen=COALESCE(?, last_seen) WHERE id=?",
                                     (json.dumps(p, ensure_ascii=False), seen_at if touch else None, row[0]))
                    self._db.execute("DELETE FROM products_fts WHERE rowid=?", (row[0],))
                    rowid = row[0]
                else:
                    cur = self._db.execute("INSERT INTO products(key, site, data, last_seen) VALUES (?,?,?,?)",
                                           (key, p["site"], json.dumps(p, ensure_ascii=False), seen_at))
                    rowid = cur.lastrowid
                self._db.execute("INSERT INTO products_fts(rowid, text) VALUES (?,?)", (rowid, _fts_text(p)))

    def _delete_key(self, key):
        row = self._db.execute("SELECT id FROM products WHERE key=?", (key,)).fetchone()
        if row:
            self._db.execute("DELETE FROM products_fts WHERE rowid=?", (row[0],))
            self._db.execute("DELETE FROM products WHERE id=?", (row[0],))

    def upsert_results(self, results, touch=True):
        self.upsert([p.to_dict() for r in results if r.ok for p in r.items + r.out_of_stock], touch)

    def search(self, query, sites=None, max_age_days=None, limit=3000):
        """{site_id: [(product dict, last_seen)]} for products matching every query word."""
        match = fts_query(query)
        if not match:
            return {}
        sql = ("SELECT p.data, p.last_seen, p.site FROM products_fts f JOIN products p ON p.id = f.rowid "
               "WHERE products_fts MATCH ?")
        args = [match]
        if max_age_days:
            sql += " AND p.last_seen >= ?"
            args.append(time.time() - max_age_days * 86400)
        if sites:
            sql += f" AND p.site IN ({','.join('?' * len(sites))})"
            args += list(sites)
        sql += " ORDER BY f.rank LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        out = {}
        for data, last_seen, site in rows:
            out.setdefault(site, []).append((json.loads(data), last_seen))
        return out

    def stats(self):
        with self._lock:
            rows = self._db.execute(
                "SELECT site, COUNT(*), MIN(last_seen), MAX(last_seen) FROM products GROUP BY site").fetchall()
        return {site: {"products": n, "oldest": lo, "newest": hi} for site, n, lo, hi in rows}

    def drop_site(self, site_id):
        """Forget a site's products, e.g. after its login changed (guest results had no prices)."""
        with self._lock, self._db:
            ids = [r[0] for r in self._db.execute("SELECT id FROM products WHERE site=?", (site_id,))]
            for i in ids:
                self._db.execute("DELETE FROM products_fts WHERE rowid=?", (i,))
            self._db.execute("DELETE FROM products WHERE site=?", (site_id,))
        return len(ids)

    def prune(self, older_than_days):
        cutoff = time.time() - older_than_days * 86400
        with self._lock, self._db:
            ids = [r[0] for r in self._db.execute("SELECT id FROM products WHERE last_seen < ?", (cutoff,))]
            for i in ids:
                self._db.execute("DELETE FROM products_fts WHERE rowid=?", (i,))
                self._db.execute("DELETE FROM products WHERE id=?", (i,))
        return len(ids)

    def close(self):
        with self._lock:
            self._db.close()


def index_results(agg, hits, sites):
    """Index hits -> one SiteResult per site, so the rest of the search pipeline is unchanged."""
    results = []
    for site in sites:
        rows = hits.get(site["id"], [])
        res = SiteResult(site["id"], site["name"], ok=True, cached=True, from_index=True)
        res.items = [Product(**{k: v for k, v in p.items() if k in _PRODUCT_FIELDS}) for p, _ in rows]
        if rows:
            res.index_age = time.time() - min(seen for _, seen in rows)
        results.append(res)
    return results


async def find(agg, index, query, only=None, source="auto", refresh=False, max_age_days=3):
    """The one search path for the UI and the AI tools.

    source: "auto"  - the index; sites it has nothing for go live (all of them when
                      the index has no match at all, or a site was never indexed)
            "index" - the index only;  "live" - the supplier sites
    Live results are always added to the index.
    """
    sites = agg.enabled_sites(only)
    if source == "live" or refresh or index is None:
        live_sites, from_index = sites, []
    else:
        hits = await asyncio.to_thread(index.search, query, only, max_age_days)
        indexed = set((await asyncio.to_thread(index.stats)).keys())
        if source == "index":
            return index_results(agg, hits, [s for s in sites if s["id"] in indexed])
        if not hits:
            live_sites, from_index = sites, []
        else:
            # a crawled site without a match has nothing to find; a never-indexed one is asked live
            from_index = [s for s in sites if s["id"] in indexed]
            live_sites = [s for s in sites if s["id"] not in indexed]
    results = index_results(agg, hits, from_index) if from_index else []
    if live_sites:
        live = await agg.search(query, only={s["id"] for s in live_sites}, refresh=refresh)
        if index is not None:
            await asyncio.to_thread(index.upsert_results, live)
        results += live
    return results


class Crawler:
    """Background crawl that keeps the index covering the suppliers' catalogs."""

    def __init__(self, agg, index, settings):
        self.agg = agg
        self.index = index
        self.settings = {**DEFAULT_SETTINGS, **(settings or {})}
        self.queries = self.settings.get("queries") or DEFAULT_QUERIES
        self.status = {"running": False, "started": None, "finished": None, "progress": {},
                       "errors": [], "last_added": 0}
        self._task = None
        self._job = None

    def start(self):
        if self.settings["enabled"] and self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def _loop(self):
        # crawl when due; on Fly a suspended machine just catches up when it wakes
        while True:
            last = self.status["finished"] or self._last_crawl_from_index()
            if not last or time.time() - last > float(self.settings["every_hours"]) * 3600:
                await self.run()
            await asyncio.sleep(60)

    def _last_crawl_from_index(self):
        newest = [s["newest"] for s in self.index.stats().values()]
        return max(newest) if newest else None

    async def run(self, only=None):
        if self.status["running"]:
            return False
        before = sum(s["products"] for s in self.index.stats().values())
        self.status.update(running=True, started=time.time(), finished=None, errors=[], progress={})

        async def crawl():
            await asyncio.gather(*(self._crawl_site(s) for s in self.agg.enabled_sites(only)))
            await asyncio.to_thread(self.index.prune, float(self.settings["prune_days"]))

        # the crawl is its own task so cancel() stops it whoever started it (button or schedule)
        self._job = asyncio.create_task(crawl())
        try:
            await self._job
        except asyncio.CancelledError:
            if not self._job.cancelled():
                self._job.cancel()
                raise  # we are being shut down
            self.status["errors"].append("הופסק ידנית")
        finally:
            self._job = None
            after = sum(s["products"] for s in self.index.stats().values())
            self.status.update(running=False, finished=time.time(), last_added=after - before)
        return True

    def cancel(self):
        """Stop a running crawl; what it already collected stays in the index."""
        if self._job is not None and not self._job.done():
            self._job.cancel()
            return True
        return False

    async def _crawl_site(self, site):
        queries = site.get("index_queries") or self.queries
        progress = self.status["progress"][site["id"]] = {"done": 0, "total": len(queries), "found": 0}
        for q in queries:
            try:
                res = await self.agg.search_site(site, q, refresh=True, pages=int(self.settings["pages"]))
                if self.settings["deep"]:
                    await self.agg.deepen([res])
                await asyncio.to_thread(self.index.upsert_results, [res])
                progress["found"] += len(res.items)
                if res.error and not res.needs_login:
                    self.status["errors"].append(f"{site['name']} / {q}: {res.error}"[:200])
            except Exception as e:  # one failing query never stops the crawl
                self.status["errors"].append(f"{site['name']} / {q}: {type(e).__name__}"[:200])
            progress["done"] += 1
            await asyncio.sleep(float(self.settings["pause_seconds"]))  # be polite to the supplier

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
