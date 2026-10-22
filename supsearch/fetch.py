"""Getting pages from the supplier sites.

Two ways, chosen per site with `fetch:` in sites.yaml:
  http     plain HTTPS request; fast, for open sites
  browser  a real Chrome via Playwright, for sites behind a Cloudflare challenge,
           JS-rendered results, or a dealer login

With settings.proxy set (OUTBOUND_PROXY), both ways go through that proxy,
except for sites marked `direct: true`.

Browser sites each get their own persistent profile under data/profiles/<id>,
so Cloudflare clearance and login cookies survive restarts. A site that needs a
human (captcha, SMS code) can be opened in a visible window once with
open_interactive(); afterwards the headless searches reuse that session.
"""

import asyncio
import os
import ssl

import httpx

from .config import DATA_DIR

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
    "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
}


CHALLENGE_TITLES = ("just a moment", "attention required", "checking your browser",
                    "עבור לדף המבוקש", "please wait", "ddos")


# firewall rejection pages (F5 "Request Rejected" and the like); retried, then reported
BLOCKED_TITLES = ("request rejected", "access denied", "error page", "403 forbidden")


class NeedsLogin(Exception):
    """The site shows prices only to a logged-in dealer and login did not succeed.

    Carries the page as a guest saw it, for sites whose login is `optional`."""

    def __init__(self, message, content=None):
        super().__init__(message)
        self.content = content


def site_proxy(settings, site):
    return None if site.get("direct") else (settings.get("proxy") or None)


class HttpFetcher:
    def __init__(self, settings=None):
        self.settings = settings or {}
        self._clients = {}

    def _client(self, site, timeout):
        client = self._clients.get(site["id"])
        if client is None:
            verify = True
            if site.get("tls_legacy"):
                # some sites still use a DH key OpenSSL 3 rejects by default;
                # certificate verification stays on, only the cipher floor is lowered
                verify = ssl.create_default_context()
                verify.set_ciphers("DEFAULT@SECLEVEL=0")
            client = httpx.AsyncClient(headers={**HEADERS, **(site.get("headers") or {})},
                                       follow_redirects=True, verify=verify, timeout=timeout,
                                       proxy=site_proxy(self.settings, site))
            self._clients[site["id"]] = client
        return client

    async def get(self, site, url, timeout):
        try:
            r = await self._client(site, timeout).get(url)
        except httpx.TransportError:
            # after a Fly suspend/resume the pooled connections are dead but look alive;
            # one retry goes out on a fresh connection
            r = await self._client(site, timeout).get(url)
        if r.status_code in (401, 403) and "cf-mitigated" in r.headers:
            raise RuntimeError("חסום ע\"י Cloudflare - יש להעביר את האתר ל-fetch: browser")
        r.raise_for_status()
        return r.text

    async def close(self):
        for c in self._clients.values():
            await c.aclose()
        self._clients.clear()


class BrowserPool:
    def __init__(self, settings):
        self.settings = settings
        self._pw = None
        self._contexts = {}
        self._locks = {}
        self._interactive = set()
        self._prepared = set()  # sites whose setup_url was visited in the current context

    async def _playwright(self):
        if self._pw is None:
            from playwright.async_api import async_playwright
            self._pw = await async_playwright().start()
        return self._pw

    async def _launch(self, site_id, headless, proxy=None):
        pw = await self._playwright()
        profile = os.path.join(DATA_DIR, "profiles", site_id)
        os.makedirs(profile, exist_ok=True)
        opts = dict(user_data_dir=profile, headless=headless, locale="he-IL",
                    viewport={"width": 1366, "height": 900},
                    args=["--disable-blink-features=AutomationControlled"])
        if proxy:
            opts["proxy"] = {"server": proxy}
        if headless:
            # headless Chrome announces itself as "HeadlessChrome", which some stores block
            opts["user_agent"] = UA
        exe = os.environ.get("BROWSER_EXECUTABLE")
        if exe:
            return await pw.chromium.launch_persistent_context(executable_path=exe, **opts)
        channel = self.settings.get("channel")
        if channel:
            try:
                return await pw.chromium.launch_persistent_context(channel=channel, **opts)
            except Exception:
                pass  # Chrome not installed: use Playwright's own Chromium
        return await pw.chromium.launch_persistent_context(**opts)

    async def context(self, site_id, proxy=None):
        lock = self._locks.setdefault(site_id, asyncio.Lock())
        async with lock:
            if site_id in self._interactive:
                raise RuntimeError("חלון התחברות פתוח לאתר הזה - סגור אותו כדי לחפש")
            ctx = self._contexts.get(site_id)
            if ctx is None:
                ctx = await self._launch(site_id, self.settings.get("headless", True), proxy)
                self._contexts[site_id] = ctx
            return ctx

    async def get(self, site, url, timeout, credentials=None, check_login=True):
        """Load one results page. check_login=False for page 2+, which on some
        sites is a bare fragment without the header that shows the login state."""
        ctx = await self.context(site["id"], site_proxy(self.settings, site))
        page = await ctx.new_page()
        try:
            if site.get("setup_url") and site["id"] not in self._prepared:
                # e.g. switching the store's display currency to shekels, once per session
                await page.goto(site["setup_url"], wait_until="domcontentloaded", timeout=timeout * 1000)
                self._prepared.add(site["id"])
            html = await self._load(page, site, url, timeout)
            login = site.get("login")
            if check_login and login and login.get("logged_in") and not await page.query_selector(login["logged_in"]):
                if not credentials or not login.get("username"):
                    raise NeedsLogin("לא מחובר - מחירים מוסתרים. לחץ 'התחברות' או הגדר פרטי כניסה", html)
                await self._login(page, login, credentials, timeout)
                html = await self._load(page, site, url, timeout)
                if not await page.query_selector(login["logged_in"]):
                    raise NeedsLogin("ההתחברות נכשלה - בדוק את הפרטים או לחץ 'התחברות'", html)
            return html
        finally:
            await page.close()

    async def _load(self, page, site, url, timeout):
        retries = int(site.get("retries", 0))
        for attempt in range(retries + 1):
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            title = (await page.title()).lower()
            if any(w in title for w in CHALLENGE_TITLES):
                break  # a challenge page solves itself below; reloading would restart it
            blocked = (resp is not None and resp.status in (403, 429, 503)) or \
                any(w in title for w in BLOCKED_TITLES)
            if not blocked:
                break
            if attempt == retries:
                raise RuntimeError("חומת האש של האתר חסמה את הבקשה - נסה שוב בעוד רגע")
            await asyncio.sleep(2 * (attempt + 1))  # firewalls that block intermittently
        if site.get("format") == "json":
            return await resp.text()
        wait_for = site.get("wait_for") or site.get("item")
        try:
            await page.wait_for_load_state("load", timeout=timeout * 1000 // 2)
        except Exception:
            pass
        if not await page.query_selector(wait_for):
            # A challenge/interstitial page resolves itself within seconds in a real
            # browser, so wait long for it; otherwise give late scripts a moment only,
            # since "no results" is a valid outcome.
            title = (await page.title()).lower()
            challenge = any(w in title for w in CHALLENGE_TITLES)
            try:
                await page.wait_for_selector(wait_for, timeout=(timeout * 1000 * 2 // 3) if challenge else 3000)
            except Exception:
                pass
        return await page.content()

    async def _login(self, page, login, credentials, timeout):
        await page.goto(login["url"], wait_until="domcontentloaded", timeout=timeout * 1000)
        for selector in (login.get("dismiss"), login.get("open")):
            # a site-wide popup to close, then the button that opens a login dialog
            if selector and await page.query_selector(selector):
                try:
                    await page.click(selector, timeout=5000)
                except Exception:
                    pass
        await page.wait_for_selector(login["username"], timeout=timeout * 1000 // 2)
        await page.fill(login["username"], str(credentials.get("username", "")))
        await page.fill(login["password"], str(credentials.get("password", "")))
        if login.get("submit") and await page.query_selector(login["submit"]):
            await page.click(login["submit"])
        else:
            await page.press(login["password"], "Enter")
        try:
            await page.wait_for_load_state("networkidle", timeout=timeout * 1000)
        except Exception:
            pass

    async def reset(self, site_id):
        """Forget a site's session (after its saved login changed), so the next search
        logs in again with the new details."""
        lock = self._locks.setdefault(site_id, asyncio.Lock())
        async with lock:
            ctx = self._contexts.pop(site_id, None)
            if ctx is not None:
                try:
                    await ctx.clear_cookies()
                finally:
                    await ctx.close()
            self._prepared.discard(site_id)

    async def check_login(self, site, credentials, search_url, timeout):
        """Log in with fresh cookies and report (ok, message) for the admin page."""
        login = site.get("login") or {}
        if not login.get("username"):
            return False, "לאתר לא הוגדר טופס התחברות - התחברות ידנית בלבד"
        await self.reset(site["id"])
        ctx = await self.context(site["id"], site_proxy(self.settings, site))
        page = await ctx.new_page()
        try:
            await self._login(page, login, credentials, timeout)
            if not login.get("logged_in"):
                return True, "פרטי הכניסה נשלחו (לאתר אין דרך לאמת שההתחברות הצליחה)"
            await self._load(page, site, search_url, timeout)
            if await page.query_selector(login["logged_in"]):
                return True, "ההתחברות הצליחה"
            return False, ("לא זוהתה התחברות - בדוק שם משתמש וסיסמה "
                           "(או שהאתר דורש קוד/captcha)")
        except Exception as e:
            return False, f"שגיאה בהתחברות: {type(e).__name__}: {e}"[:300]
        finally:
            await page.close()

    async def open_interactive(self, site):
        """Show the site in a visible window so the user can log in / pass a captcha.

        Returns once the window is closed; the session is kept in the profile."""
        site_id = site["id"]
        lock = self._locks.setdefault(site_id, asyncio.Lock())
        async with lock:
            if site_id in self._interactive:
                return
            ctx = self._contexts.pop(site_id, None)
            if ctx is not None:
                await ctx.close()
            self._prepared.discard(site_id)
            self._interactive.add(site_id)
        try:
            ctx = await self._launch(site_id, headless=False, proxy=site_proxy(self.settings, site))
            self._record_pages(ctx, site_id)
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            start = (site.get("login") or {}).get("url") or site.get("home") or site["search_url"].split("?")[0]
            try:
                await page.goto(start)
            except Exception:
                pass  # the user can still navigate by hand
            closed = asyncio.Event()
            ctx.on("close", lambda *_: closed.set())
            await closed.wait()
        finally:
            self._interactive.discard(site_id)

    def _record_pages(self, ctx, site_id):
        """Save every page the user opens in the visible window to data/captures/<site>/.

        That is how a new site gets configured: log in, run one search by hand,
        close the window, and send the saved pages to whoever writes the selectors."""
        folder = os.path.join(DATA_DIR, "captures", site_id)
        os.makedirs(folder, exist_ok=True)
        counter = [len(os.listdir(folder))]

        async def save(page):
            try:
                html = await page.content()
            except Exception:
                return
            counter[0] += 1
            path = os.path.join(folder, f"{counter[0]:03d}.html")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(f"<!-- captured from: {page.url} -->\n{html}")

        def watch(page):
            page.on("load", lambda p: asyncio.ensure_future(save(p)))

        for p in ctx.pages:
            watch(p)
        ctx.on("page", watch)

    def is_interactive(self, site_id):
        return site_id in self._interactive

    async def close(self):
        for ctx in list(self._contexts.values()):
            try:
                await ctx.close()
            except Exception:
                pass
        self._contexts.clear()
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None
