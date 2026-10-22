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

MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_ROUNDS = 6           # tool rounds before Claude must answer with what it found
RESULTS_PER_SEARCH = 30  # products returned to Claude per search
# USD per million tokens for MODEL, to show what a search cost
PRICE_IN, PRICE_OUT, PRICE_CACHE_READ, PRICE_CACHE_WRITE = 4.00, 20.00, 0.20, 5.00

SYSTEM = """You help the buyer of an Israeli computer shop and lab find products at their distributors \
(Techno-Rezef, Visual, Amtel, Mor Levi, C-Data and others). The buyer writes in Hebrew or English: a \
product description, a model name, a manufacturer part number, or a customer's requirement.

Find the product they asked for, or the closest products the distributors actually have, preferring \
items in stock. Use search_suppliers as many times as useful: the suppliers' own search is keyword \
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
            "(before and including VAT, null when hidden), stock (true/false/null when unknown) and the "
            "specs read from the title. Results are relevance-sorted, at most 30."),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "Short keyword query, e.g. a part number or 'lenovo i5 16GB'"},
                "in_stock_only": {"type": "boolean",
                                  "description": "Drop products the supplier marks out of stock"},
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

    def __init__(self, agg, sites):
        self.agg = agg
        self.sites = sites
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

    async def search_suppliers(self, query, in_stock_only):
        query = query.strip()
        if not query:
            return "Error: empty query", True
        self.searches.append(query)
        results = await self.agg.search(query, only=self.sites)
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

    async def run_tool(self, block):
        try:
            if block.name == "search_suppliers":
                text, err = await self.search_suppliers(block.input["query"], block.input["in_stock_only"])
            elif block.name == "product_details":
                text, err = await self.product_details(block.input["product_id"])
            else:
                text, err = f"Error: unknown tool {block.name}", True
        except Exception as e:  # a failing tool is reported to Claude, not raised
            text, err = f"Error: {type(e).__name__}: {e}"[:500], True
        result = {"type": "tool_result", "tool_use_id": block.id, "content": text}
        if err:
            result["is_error"] = True
        return result


async def ai_search(agg, api_key, request_text, in_stock_only=True, sites=None):
    """Run the search conversation. Returns the answer with full product dicts."""
    session = _Session(agg, sites)
    client = make_client(api_key)
    hint = "" if in_stock_only else "\n\n(Include out-of-stock products too.)"
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
        results = await asyncio.gather(*(session.run_tool(b) for b in calls))
        messages.append({"role": "user", "content": list(results)})  # all results in one message

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
