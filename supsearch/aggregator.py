"""Searching every enabled site at once and caching the raw results.

Each site runs concurrently with its own timeout, so one slow or broken site
only drops its own results; its error is reported next to the others.
Results are cached per (site, query) so changing filters never re-scrapes.
"""

import asyncio
import time

import httpx
from urllib.parse import quote_plus

from .fetch import BrowserPool, HttpFetcher, NeedsLogin
from .models import SiteResult
from .parse import parse_results

BOI_RATE_URL = "https://boi.org.il/PublicApi/GetExchangeRate?key={currency}"


def page_urls(site, query):
    q = quote_plus(query.strip())
    urls = [site["search_url"].replace("{query}", q)]
    if site.get("page_url"):
        for n in range(2, int(site.get("pages", 1)) + 1):
            urls.append(site["page_url"].replace("{query}", q).replace("{page}", str(n)))
    return urls


class Aggregator:
    def __init__(self, settings, sites, credentials):
        self.settings = settings
        self.sites = sites
        self.credentials = credentials
        self.http = HttpFetcher()
        self.browser = BrowserPool(settings)
        self._cache = {}
        self._rates = {}  # currency -> (fetched_at, shekels per unit)

    def site(self, site_id):
        return next((s for s in self.sites if s["id"] == site_id), None)

    def enabled_sites(self, only=None):
        return [s for s in self.sites if s.get("enabled") and (not only or s["id"] in only)]

    async def rate(self, currency):
        """Bank of Israel representative rate, refreshed every 6 hours.

        Falls back to `rates:` in sites.yaml settings when the bank is unreachable."""
        hit = self._rates.get(currency)
        if hit and time.time() - hit[0] < 6 * 3600:
            return hit[1]
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(BOI_RATE_URL.format(currency=currency))
                r.raise_for_status()
                j = r.json()
                value = float(j["currentExchangeRate"]) / float(j.get("unit") or 1)
            self._rates[currency] = (time.time(), value)
            return value
        except Exception:
            if hit:
                return hit[1]
            fallback = (self.settings.get("rates") or {}).get(currency)
            return float(fallback) if fallback else None

    async def fetch(self, site, url, timeout, first_page=True):
        if site.get("fetch") == "browser":
            return await self.browser.get(site, url, timeout, self.credentials.get(site["id"]),
                                          check_login=first_page)
        return await self.http.get(site, url, timeout)

    async def search_site(self, site, query, refresh=False):
        key = (site["id"], query.strip().lower())
        ttl = float(self.settings.get("cache_minutes", 10)) * 60
        hit = self._cache.get(key)
        if hit and not refresh and time.time() - hit[0] < ttl:
            res = hit[1]
            return SiteResult(res.site, res.site_name, res.ok, res.items, res.error,
                              res.needs_login, res.elapsed, cached=True)

        timeout = int(site.get("timeout") or self.settings.get("timeout", 30))
        vat_rate = float(site.get("vat_rate") or self.settings.get("vat_rate", 0.18))
        currency = (site.get("currency") or "ILS").upper()
        rates = {currency: await self.rate(currency)} if currency != "ILS" else {}
        start = time.time()
        res = SiteResult(site["id"], site["name"], ok=True)
        try:
            async def run():
                seen = set()
                for n, url in enumerate(page_urls(site, query)):
                    try:
                        content = await self.fetch(site, url, timeout, first_page=n == 0)
                    except NeedsLogin as e:
                        if not (site.get("login") or {}).get("optional") or e.content is None:
                            raise
                        # guest view still lists the products, just without dealer prices
                        res.needs_login = True
                        res.error = str(e)
                        content = e.content
                    found = [p for p in parse_results(content, site, url, vat_rate, rates)
                             if (p.url or p.title) not in seen]
                    if not found:
                        break  # past the last page
                    seen.update(p.url or p.title for p in found)
                    res.items.extend(found)
                    if site.get("page_size") and len(found) < int(site["page_size"]):
                        break  # a short page is the last one
            await asyncio.wait_for(run(), timeout=timeout * max(1, len(page_urls(site, query))))
        except NeedsLogin as e:
            res.needs_login = True
            res.ok = False
            res.error = str(e)
        except asyncio.TimeoutError:
            res.ok = bool(res.items)
            res.error = "תם הזמן - האתר לא ענה בזמן"
        except Exception as e:  # one broken site must never break the whole search
            res.ok = bool(res.items)
            res.error = f"{type(e).__name__}: {e}"[:300]
        res.elapsed = time.time() - start
        if res.ok and not res.error and not res.needs_login:
            self._cache[key] = (time.time(), res)
        return res

    async def search(self, query, only=None, refresh=False):
        sites = self.enabled_sites(only)
        return await asyncio.gather(*(self.search_site(s, query, refresh) for s in sites))

    async def close(self):
        await self.http.close()
        await self.browser.close()
