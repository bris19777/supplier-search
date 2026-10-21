"""Web server: the Hebrew search UI and its JSON API.

Runs on the office PC (python -m supsearch). Binds to localhost by default;
set HOST=0.0.0.0 to reach it from other machines on the LAN, and then also set
APP_PASSWORD, since the server holds the dealer logins.
"""

import asyncio
import os
import secrets
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

from . import filters
from .aggregator import Aggregator, page_urls
from .config import DATA_DIR, load_credentials, load_sites
from .parse import parse_results

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_ITEMS = 600

_basic = HTTPBasic(auto_error=False)


def auth(creds: HTTPBasicCredentials = Depends(_basic)):
    if not APP_PASSWORD:
        return
    if creds is None or not secrets.compare_digest(creds.password, APP_PASSWORD):
        raise HTTPException(401, "סיסמה שגויה", headers={"WWW-Authenticate": "Basic"})


def create_app(sites_path=None, credentials_path=None):
    state = {}

    @asynccontextmanager
    async def lifespan(app):
        settings, sites = load_sites(sites_path)
        state["agg"] = Aggregator(settings, sites, load_credentials(credentials_path))
        state["tasks"] = set()
        yield
        await state["agg"].close()

    app = FastAPI(title="חיפוש ספקים", lifespan=lifespan, dependencies=[Depends(auth)])

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

    class CaptureRequest(BaseModel):
        query: str

    @app.get("/")
    async def index():
        return FileResponse(os.path.join(STATIC, "index.html"))

    @app.get("/api/sites")
    async def sites():
        agg = state["agg"]
        return [{
            "id": s["id"], "name": s["name"], "enabled": bool(s.get("enabled")),
            "fetch": s.get("fetch"), "home": s.get("home", ""),
            "login": bool(s.get("login")), "has_credentials": s["id"] in agg.credentials,
            "interactive": agg.browser.is_interactive(s["id"]), "notes": s.get("notes", ""),
        } for s in agg.sites]

    @app.post("/api/search")
    async def search(req: SearchRequest):
        query = req.query.strip()
        if not query:
            raise HTTPException(400, "יש להזין מילת חיפוש")
        agg = state["agg"]
        results = await agg.search(query, only=set(req.sites) or None, refresh=req.refresh)
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
    async def login_window(site_id: str):
        agg = state["agg"]
        site = agg.site(site_id)
        if site is None:
            raise HTTPException(404, "אתר לא קיים")
        if site.get("fetch") != "browser":
            raise HTTPException(400, "האתר מוגדר כ-fetch: http ואינו משתמש בדפדפן")
        if agg.browser.is_interactive(site_id):
            return {"ok": True, "message": "חלון ההתחברות לאתר הזה כבר פתוח במחשב השרת."}
        task = asyncio.create_task(agg.browser.open_interactive(site))
        state["tasks"].add(task)
        task.add_done_callback(state["tasks"].discard)
        return {"ok": True, "message": "חלון דפדפן נפתח במחשב השרת. התחבר וסגור את החלון בסיום."}

    @app.post("/api/sites/{site_id}/capture")
    async def capture(site_id: str, req: CaptureRequest):
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
        folder = os.path.join(DATA_DIR, "captures")
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
