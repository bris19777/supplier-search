"""Web server: the Hebrew search UI and its JSON API.

Runs on the office PC (python -m supsearch) or on a cloud host (Dockerfile,
fly.toml). Binds to localhost by default; set HOST=0.0.0.0 to reach it from
other machines.

Access is by user accounts (store.py): admins manage the users and the shared
supplier logins, users search. The first admin comes from ADMIN_PASSWORD (or
APP_PASSWORD) on first start. With neither set and no users yet, the server is
open to requests from the machine itself only (the office PC's own browser)
until an admin is created in the admin page. REQUIRE_PASSWORD=1 (set on Fly)
never runs open.
"""

import asyncio
import os
import sys
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from . import ai, config, filters, quote
from .index import Crawler, Index, find
from .aggregator import Aggregator, page_urls
from .config import load_credentials, load_sites
from .parse import parse_results
from .store import Credentials, Store, StoreError

REQUIRE_PASSWORD = os.environ.get("REQUIRE_PASSWORD") == "1"
# A visible login window needs a screen: not on a headless Linux server
_interactive_env = os.environ.get("INTERACTIVE_LOGIN", "auto")
INTERACTIVE = _interactive_env == "1" or (
    _interactive_env == "auto" and not (sys.platform.startswith("linux") and not os.environ.get("DISPLAY")))
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_ITEMS = 600
SESSION_COOKIE = "supsearch_session"
OPEN_ADMIN = {"username": "", "role": "admin", "open": True}  # no users yet, office PC only
MAX_FAILURES, FAILURE_WINDOW = 5, 15 * 60  # login attempts per user+address


def client_ip(request):
    return (request.headers.get("fly-client-ip")
            or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else ""))


def is_local(request):
    """A request from the server machine itself, not relayed by a proxy."""
    host = request.client.host if request.client else ""
    forwarded = request.headers.get("x-forwarded-for") or request.headers.get("fly-client-ip")
    return host in ("127.0.0.1", "::1") and not forwarded


def create_app(sites_path=None, credentials_path=None):
    state = {}
    failures = {}  # (ip, username) -> [timestamps]

    @asynccontextmanager
    async def lifespan(app):
        settings, sites = load_sites(sites_path)
        store = Store(config.DATA_DIR, os.environ.get("SECRET_KEY"))
        first_password = os.environ.get("ADMIN_PASSWORD") or os.environ.get("APP_PASSWORD")
        if not store.has_users() and first_password:
            store.create_user(os.environ.get("ADMIN_USERNAME", "admin"), first_password, "admin")
        state["store"] = store
        state["agg"] = Aggregator(settings, sites, Credentials(store, load_credentials(credentials_path)))
        state["tasks"] = set()
        state["index"] = Index(os.path.join(config.DATA_DIR, "index.sqlite"))
        state["crawler"] = Crawler(state["agg"], state["index"], settings["index"])
        state["crawler"].start()
        yield
        await state["crawler"].stop()
        await state["agg"].close()
        state["index"].close()

    def current_user(request):
        store = state["store"]
        if not store.has_users():
            return OPEN_ADMIN if not REQUIRE_PASSWORD and is_local(request) else None
        return store.session_user(request.cookies.get(SESSION_COOKIE))

    def require_user(request: Request):
        user = current_user(request)
        if user is None:
            if not state["store"].has_users():
                raise HTTPException(503, "השרת נעול: אין עדיין משתמשים. יש להגדיר APP_PASSWORD, "
                                         "או ליצור מנהל מהדפדפן של מחשב השרת עצמו")
            raise HTTPException(401, "יש להתחבר")
        return user

    def require_admin(user=Depends(require_user)):
        if user["role"] != "admin":
            raise HTTPException(403, "פעולה למנהלים בלבד")
        return user

    def page(name, admin=False):
        async def handler(request: Request):
            user = current_user(request)
            if user is None:
                if not state["store"].has_users():
                    require_user(request)  # raises the "locked, no users yet" 503
                return RedirectResponse("login", status_code=303)
            if admin and user["role"] != "admin":
                return RedirectResponse("./", status_code=303)
            return FileResponse(os.path.join(STATIC, name))
        return handler

    app = FastAPI(title="חיפוש ספקים", lifespan=lifespan)
    app.get("/")(page("index.html"))
    app.get("/admin")(page("admin.html", admin=True))
    app.get("/ai")(page("ai.html"))

    AI_KEY = "anthropic_api_key"

    def ai_key():
        """The admin-saved key, else ANTHROPIC_API_KEY from the environment."""
        return state["store"].get_secret(AI_KEY) or os.environ.get("ANTHROPIC_API_KEY") or None

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/login")
    async def login_page():
        return FileResponse(os.path.join(STATIC, "login.html"))

    # -- sessions ----------------------------------------------------------------

    class LoginRequest(BaseModel):
        username: str
        password: str

    @app.post("/api/login")
    async def login(req: LoginRequest, request: Request, response: Response):
        key = (client_ip(request), req.username.strip().lower())
        now = time.time()
        recent = [t for t in failures.get(key, []) if now - t < FAILURE_WINDOW]
        if len(recent) >= MAX_FAILURES:
            raise HTTPException(429, "יותר מדי ניסיונות כושלים - נסה שוב בעוד רבע שעה")
        user = state["store"].authenticate(req.username, req.password)
        if user is None:
            failures[key] = recent + [now]
            raise HTTPException(401, "שם משתמש או סיסמה שגויים")
        failures.pop(key, None)
        token = state["store"].new_session(user["username"])
        https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
        response.set_cookie(SESSION_COOKIE, token, max_age=30 * 86400, httponly=True,
                            samesite="lax", secure=https)
        return user

    @app.post("/api/logout")
    async def logout(request: Request, response: Response):
        state["store"].end_session(request.cookies.get(SESSION_COOKIE))
        response.delete_cookie(SESSION_COOKIE)
        return {"ok": True}

    @app.get("/api/me")
    async def me(user=Depends(require_user)):
        favorites = [] if user.get("open") else state["store"].get_favorites(user["username"])
        return {"username": user["username"], "role": user["role"], "open": bool(user.get("open")),
                "favorites": favorites}

    class Favorites(BaseModel):
        sites: list[str]

    @app.put("/api/me/favorites")
    async def set_favorites(req: Favorites, user=Depends(require_user)):
        if user.get("open"):
            raise HTTPException(400, "ספקים מועדפים נשמרים למשתמש - צריך קודם ליצור משתמשים")
        known = {s["id"] for s in state["agg"].sites}
        state["store"].set_favorites(user["username"], [s for s in req.sites if s in known])
        return {"favorites": state["store"].get_favorites(user["username"])}

    # -- price quotes (PDF) -----------------------------------------------------------

    class QuoteItem(BaseModel):
        title: str = Field(min_length=1, max_length=300)
        mpn: str = Field("", max_length=80)
        image: str = Field("", max_length=1000)
        qty: float = Field(gt=0, le=10000)
        unit_price: float = Field(ge=0, le=10_000_000)  # selling price per unit, before VAT

    class QuoteRequest(BaseModel):
        items: list[QuoteItem] = Field(min_length=1, max_length=50)
        customer: str = Field("", max_length=120)
        customer_contact: str = Field("", max_length=200)
        notes: str = Field("", max_length=1000)
        show_images: bool = True

    class QuoteSettings(BaseModel):
        business_name: str = Field("", max_length=120)
        business_id: str = Field("", max_length=40)
        phone: str = Field("", max_length=60)
        email: str = Field("", max_length=120)
        address: str = Field("", max_length=200)
        logo: str = ""  # data:image/... URL
        footer: str = Field("", max_length=300)
        markup_percent: float = Field(15.0, ge=0, le=500)
        valid_days: int = Field(14, ge=1, le=365)

    @app.get("/api/quote/settings")
    async def quote_settings(user=Depends(require_user)):
        return state["store"].quote_settings()

    @app.put("/api/admin/quote-settings")
    async def save_quote_settings(req: QuoteSettings, admin=Depends(require_admin)):
        try:
            state["store"].set_quote_settings(req.model_dump())
        except StoreError as e:
            raise HTTPException(400, str(e))
        return {"ok": True}

    @app.post("/api/quote/pdf")
    async def quote_pdf(req: QuoteRequest, user=Depends(require_user)):
        items = []
        for it in req.items:
            image = it.image if it.image.startswith(("https://", "http://")) else ""  # never file:// etc.
            items.append({**it.model_dump(), "image": image})
        store, agg = state["store"], state["agg"]
        vat_rate = float(agg.settings.get("vat_rate", 0.18))
        _, _, total = quote.totals(items, vat_rate)
        number = store.next_quote(user["username"] or "admin", req.customer, total)
        page = quote.quote_html(store.quote_settings(), number, items, req.customer, req.customer_contact,
                                req.notes, req.show_images, vat_rate)
        try:
            pdf = await agg.browser.render_pdf(page)
        except Exception as e:
            raise HTTPException(502, f"יצירת ה-PDF נכשלה: {type(e).__name__}"[:200])
        return Response(pdf, media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="quote-{number}.pdf"'})

    class PasswordChange(BaseModel):
        current: str
        new: str

    @app.post("/api/me/password")
    async def change_password(req: PasswordChange, user=Depends(require_user)):
        store = state["store"]
        if user.get("open") or store.authenticate(user["username"], req.current) is None:
            raise HTTPException(400, "הסיסמה הנוכחית שגויה")
        try:
            store.update_user(user["username"], password=req.new)
        except StoreError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "message": "הסיסמה הוחלפה - יש להתחבר מחדש"}

    # -- admin: users --------------------------------------------------------------

    class NewUser(BaseModel):
        username: str
        password: str
        role: str = "user"

    class UserUpdate(BaseModel):
        password: str | None = None
        role: str | None = None

    @app.get("/api/admin/users")
    async def list_users(admin=Depends(require_admin)):
        return state["store"].list_users()

    @app.post("/api/admin/users")
    async def create_user(req: NewUser, admin=Depends(require_admin)):
        try:
            user = state["store"].create_user(req.username, req.password, req.role)
        except StoreError as e:
            raise HTTPException(400, str(e))
        # the first account ends the open (no users) mode: log in from now on
        return {**user, "login_required": bool(admin.get("open"))}

    @app.patch("/api/admin/users/{username}")
    async def update_user(username: str, req: UserUpdate, admin=Depends(require_admin)):
        try:
            state["store"].update_user(username, password=req.password or None, role=req.role)
        except StoreError as e:
            raise HTTPException(400, str(e))
        return {"ok": True}

    @app.delete("/api/admin/users/{username}")
    async def delete_user(username: str, admin=Depends(require_admin)):
        if username == admin["username"]:
            raise HTTPException(400, "אי אפשר למחוק את המשתמש שאיתו אתה מחובר")
        try:
            state["store"].delete_user(username)
        except StoreError as e:
            raise HTTPException(400, str(e))
        return {"ok": True}

    # -- admin: shared supplier logins ---------------------------------------------

    class SupplierLogin(BaseModel):
        username: str
        password: str = ""  # empty keeps the saved password (username-only edit)

    def known_site(site_id):
        site = state["agg"].site(site_id)
        if site is None:
            raise HTTPException(404, "אתר לא קיים")
        return site

    @app.get("/api/admin/suppliers")
    async def list_suppliers(admin=Depends(require_admin)):
        agg, saved = state["agg"], state["store"].supplier_status()
        return [{
            "id": s["id"], "name": s["name"], "enabled": bool(s.get("enabled")),
            "home": s.get("home", ""),
            "auto_login": bool((s.get("login") or {}).get("username")),
            "saved": saved.get(s["id"]),
            "from_config": s["id"] in agg.credentials.static,
        } for s in agg.sites]

    @app.put("/api/admin/suppliers/{site_id}")
    async def save_supplier(site_id: str, req: SupplierLogin, admin=Depends(require_admin)):
        known_site(site_id)
        try:
            state["store"].set_supplier_login(site_id, req.username, req.password,
                                              by=admin["username"] or "admin")
        except StoreError as e:
            raise HTTPException(400, str(e))
        await state["agg"].forget(site_id)
        await asyncio.to_thread(state["index"].drop_site, site_id)  # re-index with the new login
        return {"ok": True}

    @app.delete("/api/admin/suppliers/{site_id}")
    async def delete_supplier(site_id: str, admin=Depends(require_admin)):
        known_site(site_id)
        state["store"].delete_supplier_login(site_id)
        await state["agg"].forget(site_id)
        await asyncio.to_thread(state["index"].drop_site, site_id)  # re-index with the new login
        return {"ok": True}

    @app.post("/api/admin/suppliers/{site_id}/test")
    async def test_supplier(site_id: str, admin=Depends(require_admin)):
        site = known_site(site_id)
        agg = state["agg"]
        creds = agg.credentials.get(site_id)
        if not creds:
            raise HTTPException(400, "לא נשמרו פרטי כניסה לאתר הזה")
        if site.get("fetch") != "browser" or not site.get("search_url"):
            raise HTTPException(400, "האתר עדיין לא מוגדר לחיפוש עם התחברות")
        timeout = int(site.get("timeout") or agg.settings.get("timeout", 30))
        await agg.forget(site_id)
        ok, message = await agg.browser.check_login(site, creds, page_urls(site, "test")[0], timeout)
        return {"ok": ok, "message": message}

    class SearchRequest(BaseModel):
        query: str
        sites: list[str] = Field(default_factory=list)
        refresh: bool = False
        view: str = "list"          # list | compare
        facets: dict[str, list[str]] = Field(default_factory=dict)
        price_min: float | None = None
        price_max: float | None = None
        in_stock: bool = False
        has_price: bool = False
        include: str = ""
        exclude: str = ""
        min_relevance: float = 0.0
        basis: str = "inc"          # inc | ex  (price with / without VAT)
        sort: str = "relevance"
        deep: bool = False          # read missing specs from the product pages (slower)
        source: str = "auto"        # auto | index | live  (see index.find)

    class CaptureRequest(BaseModel):
        query: str

    @app.get("/api/info")
    async def info(user=Depends(require_user)):
        # set by start.sh: "office:<exit node>" or "direct:tailscale-failed"; unset = direct
        kind, _, detail = os.environ.get("EGRESS", "").partition(":")
        return {"egress": kind or "direct", "egress_detail": detail, "ai": ai_key() is not None}

    # -- AI search ---------------------------------------------------------------

    class AIRequest(BaseModel):
        query: str
        in_stock_only: bool = False  # out-of-stock never appear; this also drops unknown stock
        sites: list[str] = Field(default_factory=list)

    class AIKey(BaseModel):
        key: str

    @app.post("/api/ai/search")
    async def ai_search(req: AIRequest, user=Depends(require_user)):
        if not req.query.strip():
            raise HTTPException(400, "יש לכתוב מה מחפשים")
        if len(req.query) > 4000:
            raise HTTPException(400, "הבקשה ארוכה מדי")
        key = ai_key()
        if not key:
            raise HTTPException(400, "חיפוש AI לא הוגדר - מנהל צריך לשמור מפתח API בעמוד הניהול")
        try:
            return await ai.ai_search(state["agg"], key, req.query, req.in_stock_only,
                                      set(req.sites) or None, index=state["index"])
        except ai.AIError as e:
            raise HTTPException(502, str(e))

    # -- admin: local index ---------------------------------------------------------

    @app.get("/api/admin/index")
    async def index_status(admin=Depends(require_admin)):
        agg, crawler = state["agg"], state["crawler"]
        stats = await asyncio.to_thread(state["index"].stats)
        return {
            "settings": {k: crawler.settings[k] for k in ("enabled", "every_hours", "pages", "deep",
                                                           "max_age_days")},
            "queries": len(crawler.queries),
            "status": crawler.status,
            "sites": [{"id": s["id"], "name": s["name"], **stats.get(s["id"], {"products": 0})}
                      for s in agg.enabled_sites()],
        }

    @app.post("/api/admin/index/run")
    async def index_run(admin=Depends(require_admin)):
        crawler = state["crawler"]
        if crawler.status["running"]:
            return {"ok": True, "message": "העדכון כבר רץ"}
        task = asyncio.create_task(crawler.run())
        state["tasks"].add(task)
        task.add_done_callback(state["tasks"].discard)
        return {"ok": True, "message": "עדכון האינדקס התחיל ברקע"}

    @app.get("/api/admin/ai-key")
    async def ai_key_status(admin=Depends(require_admin)):
        return {"saved": state["store"].secret_status(AI_KEY),
                "from_env": bool(os.environ.get("ANTHROPIC_API_KEY")), "model": ai.MODEL}

    @app.put("/api/admin/ai-key")
    async def save_ai_key(req: AIKey, admin=Depends(require_admin)):
        try:
            state["store"].set_secret(AI_KEY, req.key, by=admin["username"] or "admin")
        except StoreError as e:
            raise HTTPException(400, str(e))
        ok, message = await ai.check_key(req.key.strip())
        return {"ok": ok, "message": "נשמר. " + message}

    @app.delete("/api/admin/ai-key")
    async def delete_ai_key(admin=Depends(require_admin)):
        state["store"].delete_secret(AI_KEY)
        return {"ok": True}

    @app.post("/api/admin/ai-key/test")
    async def test_ai_key(admin=Depends(require_admin)):
        key = ai_key()
        if not key:
            raise HTTPException(400, "לא נשמר מפתח")
        ok, message = await ai.check_key(key)
        return {"ok": ok, "message": message}

    @app.get("/api/sites")
    async def sites(user=Depends(require_user)):
        agg = state["agg"]
        return [{
            "id": s["id"], "name": s["name"], "enabled": bool(s.get("enabled")),
            "fetch": s.get("fetch"), "home": s.get("home", ""),
            "login": bool(s.get("login")), "has_credentials": s["id"] in agg.credentials,
            "interactive": agg.browser.is_interactive(s["id"]), "can_open_window": INTERACTIVE and user["role"] == "admin",
            "notes": s.get("notes", ""),
        } for s in agg.sites]

    @app.post("/api/search")
    async def search(req: SearchRequest, user=Depends(require_user)):
        query = req.query.strip()
        if not query:
            raise HTTPException(400, "יש להזין מילת חיפוש")
        agg = state["agg"]
        max_age = float(agg.settings["index"].get("max_age_days", 3))
        results = await find(agg, state["index"], query, only=set(req.sites) or None,
                             source=req.source, refresh=req.refresh, max_age_days=max_age)
        if req.deep:
            await agg.deepen(results)
            # keep the specs read from product pages; not a new sighting of the price
            await asyncio.to_thread(state["index"].upsert_results, results, False)
        products = [p.to_dict() for r in results for p in r.items]
        f = req.model_dump()
        items, facets, price_range = filters.apply(products, query, f)
        body = {
            "query": query,
            "total": len(products),
            "matched": len(items),
            "sites": [r.summary() for r in results],
            "facets": facets,
            "price_range": price_range,
        }
        if req.view == "compare":
            body["groups"] = filters.group(items, req.basis)[:MAX_ITEMS]
        else:
            body["items"] = items[:MAX_ITEMS]
        return body

    @app.post("/api/sites/{site_id}/login-window")
    async def login_window(site_id: str, admin=Depends(require_admin)):
        agg = state["agg"]
        site = agg.site(site_id)
        if site is None:
            raise HTTPException(404, "אתר לא קיים")
        if site.get("fetch") != "browser":
            raise HTTPException(400, "האתר מוגדר כ-fetch: http ואינו משתמש בדפדפן")
        if not INTERACTIVE:
            raise HTTPException(400, "בשרת ענן אין מסך לחלון התחברות - שמור את פרטי הכניסה בעמוד הניהול")
        if agg.browser.is_interactive(site_id):
            return {"ok": True, "message": "חלון ההתחברות לאתר הזה כבר פתוח במחשב השרת."}
        task = asyncio.create_task(agg.browser.open_interactive(site))
        state["tasks"].add(task)
        task.add_done_callback(state["tasks"].discard)
        return {"ok": True, "message": "חלון דפדפן נפתח במחשב השרת. התחבר וסגור את החלון בסיום."}

    @app.post("/api/sites/{site_id}/capture")
    async def capture(site_id: str, req: CaptureRequest, admin=Depends(require_admin)):
        """Save the raw first results page, for writing or fixing a site's selectors."""
        agg = state["agg"]
        site = agg.site(site_id)
        if site is None:
            raise HTTPException(404, "אתר לא קיים")
        if not site.get("search_url"):
            raise HTTPException(400, "לאתר עדיין לא הוגדר search_url")
        url = page_urls(site, req.query)[0]
        timeout = int(site.get("timeout") or agg.settings.get("timeout", 30))
        try:
            content = await agg.fetch(site, url, timeout)
        except Exception as e:
            raise HTTPException(502, f"{type(e).__name__}: {e}"[:300])
        folder = os.path.join(config.DATA_DIR, "captures")
        os.makedirs(folder, exist_ok=True)
        ext = "json" if site.get("format") == "json" else "html"
        path = os.path.join(folder, f"{site_id}-{time.strftime('%Y%m%d-%H%M%S')}.{ext}")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        found = parse_results(content, site, url) if site.get("item") else []
        return {"path": path, "url": url, "bytes": len(content), "parsed": len(found),
                "sample": [p.to_dict() for p in found[:3]]}

    return app


app = create_app()
