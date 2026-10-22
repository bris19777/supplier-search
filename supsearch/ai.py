"""AI search: "find me this, or the closest thing the suppliers have in stock".

The user describes a product (free text, a model, a part number). Claude runs
searches across all suppliers through the normal aggregator (tool
`search_suppliers`), may open product pages for specs (`product_details`), and
answers with the best matches ranked exact / close / alternative, each with a
reason in Hebrew. Runs on the organisation's own Anthropic API key, saved
encrypted by an admin.
"""

import asyncio
import json

import anthropic

from . import filters
from .index import find

MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_ROUNDS = 6           # tool rounds before Claude must answer with what it found
RESULTS_PER_SEARCH = 30  # products returned to Claude per search
# USD per million tokens for MODEL, to show what a search cost
PRICE_IN, PRICE_OUT, PRICE_CACHE_READ, PRICE_CACHE_WRITE = 4.00, 20.00, 0.20, 5.00

SYSTEM = """You help the buyer of an Israeli computer shop and lab find products at their distributors \
(Techno-Rezef, Visual, Amtel, Mor Levi, C-Data and others). The buyer writes in Hebrew or English: a \
product description, a model name, a manufacturer part number, or a customer's requirement.

Find the product they asked for, or the closest products the distributors actually have in stock. \
Out-of-stock products are never returned by the tools; in_stock null means the supplier doesn't show \
stock (typically before logging in), so such an item may still be available. Use search_suppliers as many times as useful: the suppliers' own search is keyword \
based, so search short and specific (a part number, a model family like "ThinkPad L16", a key spec \
like "RTX 4060") and try variations rather than pasting the whole request. When a candidate's title \
doesn't show the specs that matter for the decision, use product_details.

Judge closeness by what matters for the buyer's request (for a laptop: CPU class, RAM, storage, \
screen size, OS; for a part: exact compatibility). The same manufacturer part number at different \
distributors is the same product - list each offer, so the buyer can compare prices. Prices may be \
missing when the system is not logged in to a supplier; then compare on specs and stock and say so.

Answer in Hebrew. Return at most 10 matches, best first, using only product ids returned by the \
tools. If nothing suitable exists, return no matches and explain in the note what is missing."""

TOOLS = [
    {
        "name": "search_suppliers",
        "description": (
            "Search all the distributors' websites at once with their own keyword search, and return "
            "the merged products: id, supplier, title, manufacturer part number, brand, price in ILS "
            "(before and including VAT, null when hidden), stock (true, or null when the supplier doesn't "
            "show it; out-of-stock products are never listed) and the specs read from the title. "
            "Results are relevance-sorted, at most 30."),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "Short keyword query, e.g. a part number or 'lenovo i5 16GB'"},
                "in_stock_only": {"type": "boolean",
                                  "description": "Only products whose stock the supplier confirms "
                                                 "(drops those with unknown stock)"},
            },
            "required": ["query", "in_stock_only"],
            "additionalProperties": False,
        },
    },
    {
        "name": "product_details",
        "description": (
            "Open one product's page at its supplier and read its specs (CPU, RAM, storage, screen, "
            "GPU, OS). Use when the title lacks a spec that matters for the decision."),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"product_id": {"type": "string", "description": "An id from search_suppliers"}},
            "required": ["product_id"],
            "additionalProperties": False,
        },
    },
]

ANSWER_FORMAT = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "understanding": {"type": "string",
                              "description": "One sentence: what the buyer is looking for"},
            "matches": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "product_id": {"type": "string"},
                        "match": {"type": "string", "enum": ["exact", "close", "alternative"]},
                        "reason": {"type": "string",
                                   "description": "Why it fits and how it differs from the request"},
                    },
                    "required": ["product_id", "match", "reason"],
                    "additionalProperties": False,
                },
            },
            "note": {"type": "string", "description": "Anything the buyer should know; may be empty"},
        },
        "required": ["understanding", "matches", "note"],
        "additionalProperties": False,
    },
}


class AIError(Exception):
    """A failure to show the user as is (Hebrew)."""


def make_client(api_key):
    return anthropic.AsyncAnthropic(api_key=api_key, timeout=240.0)


async def check_key(api_key):
    """(ok, message) - a free call that only validates the key and the model access."""
    try:
        await make_client(api_key).models.retrieve(MODEL)
        return True, "המפתח תקין"
    except anthropic.AuthenticationError:
        return False, "המפתח שגוי או בוטל"
    except anthropic.PermissionDeniedError:
        return False, "למפתח אין הרשאה למודל"
    except anthropic.APIConnectionError:
        return False, "אין חיבור ל-Anthropic"
    except anthropic.APIStatusError as e:
        return False, f"שגיאה מ-Anthropic ({e.status_code})"


def _compact(pid, p):
    return {"id": pid, "supplier": p["site_name"], "title": p["title"], "mpn": p["mpn"] or None,
            "brand": p["brand"] or None, "price_ex_vat": p["price_ex_vat"],
            "price_inc_vat": p["price_inc_vat"], "in_stock": p["in_stock"], "specs": p["specs"]}


class _Session:
    """Tool calls for one AI search; products get short ids Claude refers back to."""

    def __init__(self, agg, sites, index=None):
        self.agg = agg
        self.sites = sites
        self.index = index
        self.products = {}   # id -> product dict
        self._ids = {}       # product key -> id
        self.searches = []

    def _register(self, product):
        key = product["url"] or (product["site"] + product["title"])
        if key not in self._ids:
            pid = f"p{len(self._ids) + 1}"
            self._ids[key] = pid
            self.products[pid] = product
        return self._ids[key]

    async def _search(self, query):
        max_age = float(self.agg.settings.get("index", {}).get("max_age_days", 3))
        return await find(self.agg, self.index, query, only=self.sites, max_age_days=max_age)

    def _search_result(self, query, in_stock_only, results):
        products = [p.to_dict() for r in results for p in r.items]
        items, _, _ = filters.apply(products, query, {"in_stock": in_stock_only})
        out = [_compact(self._register(p), p) for p in items[:RESULTS_PER_SEARCH]]
        notes = [f"{r.site_name}: {r.error}" for r in results if r.error]
        return json.dumps({"total_found": len(items), "products": out, "supplier_notes": notes},
                          ensure_ascii=False), False

    async def product_details(self, product_id):
        p = self.products.get(product_id)
        if p is None:
            return f"Error: unknown product id {product_id}", True
        site = self.agg.site(p["site"])
        if not site or not site.get("detail") or not p["url"]:
            return json.dumps({"specs": p["specs"], "note": "no product page reader for this supplier"},
                              ensure_ascii=False), False
        timeout = int(site.get("timeout") or self.agg.settings.get("timeout", 30))
        try:
            specs, _ = await self.agg._detail_specs(site, p["url"], timeout)
        except Exception as e:
            return f"Error: could not open the product page ({type(e).__name__})", True
        merged = {**specs, **p["specs"]}
        p["specs"] = merged
        return json.dumps({"specs": merged}, ensure_ascii=False), False

    async def run_tools(self, calls):
        """Run one turn's tool calls. The slow part (searching, opening pages) runs in
        parallel; results are then built in call order, so product ids are deterministic."""
        async def fetch(block):
            try:
                if block.name == "search_suppliers":
                    query = block.input["query"].strip()
                    return await self._search(query) if query else ValueError("empty query")
                if block.name == "product_details":
                    return await self.product_details(block.input["product_id"])
                return ValueError(f"unknown tool {block.name}")
            except Exception as e:  # a failing tool is reported to Claude, not raised
                return e

        fetched = await asyncio.gather(*(fetch(b) for b in calls))
        out = []
        for block, got in zip(calls, fetched):
            if isinstance(got, Exception):
                text, err = f"Error: {type(got).__name__}: {got}"[:500], True
            elif block.name == "search_suppliers":
                self.searches.append(block.input["query"].strip())
                text, err = self._search_result(block.input["query"].strip(),
                                                block.input["in_stock_only"], got)
            else:
                text, err = got
            result = {"type": "tool_result", "tool_use_id": block.id, "content": text}
            if err:
                result["is_error"] = True
            out.append(result)
        return out


async def ai_search(agg, api_key, request_text, in_stock_only=False, sites=None, index=None):
    """Run the search conversation. Returns the answer with full product dicts."""
    session = _Session(agg, sites, index)
    client = make_client(api_key)
    hint = "\n\n(Only products whose stock is confirmed.)" if in_stock_only else ""
    messages = [{"role": "user", "content": request_text.strip() + hint}]
    usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    response = None
    for round_no in range(MAX_ROUNDS + 1):
        last_round = round_no == MAX_ROUNDS
        try:
            response = await client.beta.messages.create(
                model=MODEL,
                max_tokens=16000,
                betas=[FALLBACK_BETA],
                fallbacks="default",  # a declined request is retried server-side on the recommended model
                system=SYSTEM,
                tools=TOOLS,
                # last round: no more searching, answer from what was found
                tool_choice={"type": "none"} if last_round else {"type": "auto"},
                output_config={"effort": "medium", "format": ANSWER_FORMAT},
                cache_control={"type": "ephemeral"},  # each round resends the growing conversation
                messages=messages,
            )
        except anthropic.AuthenticationError:
            raise AIError("מפתח ה-API של Anthropic שגוי או בוטל - יש לעדכן אותו בעמוד הניהול")
        except anthropic.PermissionDeniedError:
            raise AIError("למפתח ה-API אין הרשאה למודל")
        except anthropic.RateLimitError:
            raise AIError("חריגה ממגבלת הקצב של Anthropic - נסה שוב בעוד דקה")
        except anthropic.BadRequestError as e:
            raise AIError(f"Anthropic דחה את הבקשה: {e.message}"[:300])
        except anthropic.APIConnectionError:
            raise AIError("אין חיבור ל-Anthropic")
        except anthropic.APIStatusError as e:
            raise AIError(f"שגיאה מ-Anthropic ({e.status_code}) - נסה שוב")

        u = response.usage
        usage["input"] += u.input_tokens or 0
        usage["output"] += u.output_tokens or 0
        usage["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
        usage["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0

        if response.stop_reason == "refusal":
            raise AIError("הבקשה נדחתה על ידי מסנני הבטיחות של Anthropic - נסח אותה אחרת")
        if response.stop_reason == "max_tokens":
            raise AIError("התשובה ארוכה מדי - נסה בקשה ממוקדת יותר")
        # keep every block (thinking included) unchanged, as the API requires
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "pause_turn":
            continue
        if response.stop_reason != "tool_use":
            break
        calls = [b for b in response.content if b.type == "tool_use"]
        results = await session.run_tools(calls)
        messages.append({"role": "user", "content": results})  # all results in one message

    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        answer = json.loads(text)
    except (ValueError, TypeError):
        raise AIError("לא התקבלה תשובה מסודרת מה-AI - נסה שוב")

    matches = []
    for m in answer.get("matches", []):
        product = session.products.get(m.get("product_id"))
        if product is not None:  # ignore ids Claude didn't get from the tools
            matches.append({**product, "match": m.get("match"), "reason": m.get("reason", "")})
    cost = (usage["input"] * PRICE_IN + usage["output"] * PRICE_OUT
            + usage["cache_read"] * PRICE_CACHE_READ + usage["cache_write"] * PRICE_CACHE_WRITE) / 1e6
    return {"understanding": answer.get("understanding", ""), "matches": matches,
            "note": answer.get("note", ""), "searches": session.searches,
            "usage": {**usage, "cost_usd": round(cost, 4)}}


# -- full technical specs from the manufacturer's own site, for price quotes ----------

# Official manufacturer sites only (subdomains included, e.g. psref.lenovo.com)
MANUFACTURER_DOMAINS = [
    "lenovo.com", "hp.com", "dell.com", "asus.com", "acer.com", "msi.com", "gigabyte.com",
    "samsung.com", "apple.com", "microsoft.com", "lg.com", "intel.com", "amd.com", "nvidia.com",
    "kingston.com", "westerndigital.com", "seagate.com", "crucial.com", "logitech.com", "tp-link.com",
    "ui.com", "epson.com", "brother.com", "canon.com", "benq.com", "viewsonic.com", "aoc.com",
    "philips.com", "dahuasecurity.com", "hikvision.com", "apc.com", "eaton.com", "synology.com",
    "qnap.com", "corsair.com", "razer.com", "steelseries.com", "jabra.com", "poly.com", "xiaomi.com",
]

SPECS_SYSTEM = """You look up the official technical specifications of one product on its manufacturer's \
own website, for a price quote an Israeli computer shop gives its customer.

Search for the exact model / manufacturer part number (for Lenovo the PSREF pages on psref.lenovo.com \
list each model by part number). Use only what the official page states for this exact model or part \
number; never fill in from similar models. Then call submit_specs once.

Write each label in Hebrew (for example: מעבד, זיכרון, אחסון, מסך, כרטיס גרפי, מערכת הפעלה, רשת \
אלחוטית, בלוטות', יציאות, מצלמה, סוללה, משקל, מידות, צבע, אחריות) and keep each value as the \
manufacturer writes it (English technical terms are fine). Aim for the full spec sheet, typically 10-25 \
rows. If no official page for this exact product can be found, call submit_specs with found=false."""

SUBMIT_SPECS = {
    "name": "submit_specs",
    "description": "Submit the specifications found on the manufacturer's official page.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "found": {"type": "boolean"},
            "model_name": {"type": "string", "description": "Model name as the manufacturer writes it"},
            "source_url": {"type": "string", "description": "The official page the specs come from"},
            "specs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"label": {"type": "string"}, "value": {"type": "string"}},
                    "required": ["label", "value"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["found", "model_name", "source_url", "specs"],
        "additionalProperties": False,
    },
}

MAX_SPEC_ROUNDS = 6  # model turns, including resuming a paused server-side search


async def manufacturer_specs(api_key, title, mpn="", brand=""):
    """Look the product up on its manufacturer's site. Returns
    {"found", "model_name", "source_url", "specs": [{label, value}], "cost_usd"}."""
    client = make_client(api_key)
    product = f"Product: {title}\nManufacturer part number: {mpn or '(unknown)'}\nBrand: {brand or '(unknown)'}"
    messages = [{"role": "user", "content": product}]
    tools = [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": 5,
         "allowed_domains": MANUFACTURER_DOMAINS},
        {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 6,
         "allowed_domains": MANUFACTURER_DOMAINS, "max_content_tokens": 30000},
        SUBMIT_SPECS,
    ]
    usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "searches": 0}
    result = None
    for _ in range(MAX_SPEC_ROUNDS):
        try:
            response = await client.beta.messages.create(
                model=MODEL, max_tokens=16000, betas=[FALLBACK_BETA], fallbacks="default",
                system=SPECS_SYSTEM, tools=tools, output_config={"effort": "medium"},
                cache_control={"type": "ephemeral"}, messages=messages)
        except anthropic.AuthenticationError:
            raise AIError("מפתח ה-API של Anthropic שגוי או בוטל - יש לעדכן אותו בעמוד הניהול")
        except anthropic.RateLimitError:
            raise AIError("חריגה ממגבלת הקצב של Anthropic - נסה שוב בעוד דקה")
        except anthropic.BadRequestError as e:
            raise AIError(f"Anthropic דחה את הבקשה: {e.message}"[:300])
        except anthropic.APIConnectionError:
            raise AIError("אין חיבור ל-Anthropic")
        except anthropic.APIStatusError as e:
            raise AIError(f"שגיאה מ-Anthropic ({e.status_code}) - נסה שוב")
        u = response.usage
        usage["input"] += u.input_tokens or 0
        usage["output"] += u.output_tokens or 0
        usage["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
        usage["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0
        stu = getattr(u, "server_tool_use", None)
        usage["searches"] += (getattr(stu, "web_search_requests", 0) or 0) if stu else 0
        if response.stop_reason == "refusal":
            raise AIError("הבקשה נדחתה על ידי מסנני הבטיחות של Anthropic")
        submitted = next((b for b in response.content if b.type == "tool_use" and b.name == "submit_specs"), None)
        if submitted is not None:
            result = dict(submitted.input)
            break
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "pause_turn":
            continue  # the server-side search loop paused; re-sending resumes it
        # answered in prose instead of calling the tool: ask once more, briefly
        messages.append({"role": "user", "content": "Call submit_specs with what you found."})
    if result is None:
        raise AIError("לא התקבל מפרט מה-AI - נסה שוב")
    specs = [{"label": str(s.get("label", "")).strip()[:80], "value": str(s.get("value", "")).strip()[:300]}
             for s in result.get("specs", []) if s.get("label") and s.get("value")][:60]
    cost = (usage["input"] * PRICE_IN + usage["output"] * PRICE_OUT + usage["cache_read"] * PRICE_CACHE_READ
            + usage["cache_write"] * PRICE_CACHE_WRITE) / 1e6 + usage["searches"] * 0.01  # $10 / 1000 searches
    return {"found": bool(result.get("found")) and bool(specs), "model_name": result.get("model_name", ""),
            "source_url": result.get("source_url", ""), "specs": specs, "cost_usd": round(cost, 4)}
