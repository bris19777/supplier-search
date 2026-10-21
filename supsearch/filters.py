"""Faceted filtering, sorting and cross-site grouping of search results.

Facet counts follow the usual store behaviour: the counts shown for one facet
are computed with every *other* active filter applied, so ticking a brand still
shows how many results each other brand would add.
"""

import re

from .normalize import norm_mpn, norm_title

# facet key -> (Hebrew label, how to read the value from a product dict)
FACETS = {
    "site": ("ספק", lambda p: p["site_name"]),
    "brand": ("יצרן", lambda p: p["brand"]),
    "category": ("קטגוריה", lambda p: p["category"]),
    "cpu": ("מעבד", lambda p: p["specs"].get("cpu", "")),
    "ram": ("זיכרון RAM", lambda p: p["specs"].get("ram", "")),
    "storage": ("נפח אחסון", lambda p: p["specs"].get("storage", "")),
    "screen": ("גודל מסך", lambda p: p["specs"].get("screen", "")),
    "gpu": ("כרטיס מסך", lambda p: p["specs"].get("gpu", "")),
    "os": ("מערכת הפעלה", lambda p: p["specs"].get("os", "")),
    "stock": ("מלאי", lambda p: {True: "במלאי", False: "לא במלאי"}.get(p["in_stock"], "לא ידוע")),
}


def _num_key(value):
    """Sort '8GB' < '16GB' < '1TB' and '13.3"' < '15.6"' naturally."""
    m = re.match(r"([\d.]+)\s*(TB|GB)?", value)
    if not m:
        return (1, value)
    try:
        n = float(m.group(1))
    except ValueError:
        return (1, value)
    return (0, n * (1024 if m.group(2) == "TB" else 1), value)


def _tokens(text):
    return [t for t in norm_title(text).split() if t]


def relevance(product, query_tokens):
    """Share of query words found in the title/part numbers (supplier search is fuzzy)."""
    if not query_tokens:
        return 1.0
    hay = norm_title(" ".join([product["title"], product["mpn"], product["sku"], product["brand"]]))
    compact = hay.replace(" ", "")
    hits = sum(1 for t in query_tokens if t in hay or t.replace(" ", "") in compact)
    return hits / len(query_tokens)


def price_of(product, basis):
    return product["price_ex_vat"] if basis == "ex" else product["price_inc_vat"]


def _passes(p, f, skip=None):
    for key, values in (f.get("facets") or {}).items():
        if key == skip or not values or key not in FACETS:
            continue
        if FACETS[key][1](p) not in values:
            return False
    price = price_of(p, f.get("basis"))
    if f.get("price_min") is not None and (price is None or price < f["price_min"]):
        return False
    if f.get("price_max") is not None and (price is None or price > f["price_max"]):
        return False
    if f.get("in_stock") and p["in_stock"] is not True:
        return False
    if f.get("has_price") and price is None:
        return False
    title = norm_title(p["title"] + " " + p["mpn"] + " " + p["sku"])
    for word in _tokens(f.get("include", "")):
        if word not in title:
            return False
    for word in _tokens(f.get("exclude", "")):
        if word in title:
            return False
    if f.get("min_relevance") and p.get("_rel", 1) < f["min_relevance"]:
        return False
    return True


def apply(products, query, f):
    """Filter and sort product dicts. Returns (items, facets, price_range)."""
    q = _tokens(query)
    for p in products:
        p["_rel"] = relevance(p, q)

    items = [p for p in products if _passes(p, f)]

    facets = []
    for key, (label, get) in FACETS.items():
        counts = {}
        for p in products:
            if _passes(p, f, skip=key):
                v = get(p)
                if v:
                    counts[v] = counts.get(v, 0) + 1
        if not counts:
            continue
        selected = set((f.get("facets") or {}).get(key) or [])
        if key in ("ram", "storage", "screen"):
            order = sorted(counts, key=_num_key)
        else:
            order = sorted(counts, key=lambda v: (-counts[v], v))
        facets.append({"key": key, "label": label, "values": [
            {"value": v, "count": counts[v], "selected": v in selected} for v in order]})

    basis = f.get("basis")
    sort = f.get("sort") or "relevance"
    no_price = float("inf")
    if sort == "price_asc":
        items.sort(key=lambda p: (price_of(p, basis) is None, price_of(p, basis) or no_price))
    elif sort == "price_desc":
        items.sort(key=lambda p: (price_of(p, basis) is None, -(price_of(p, basis) or 0)))
    elif sort == "site":
        items.sort(key=lambda p: (p["site_name"], price_of(p, basis) or no_price))
    elif sort == "title":
        items.sort(key=lambda p: p["title"].lower())
    else:
        items.sort(key=lambda p: (-p["_rel"], p["in_stock"] is not True,
                                  price_of(p, basis) is None, price_of(p, basis) or no_price))

    prices = [price_of(p, basis) for p in products if price_of(p, basis) is not None]
    price_range = [min(prices), max(prices)] if prices else None
    return items, facets, price_range


def group_key(p):
    mpn = norm_mpn(p["mpn"])
    return ("mpn:" + mpn) if len(mpn) >= 5 else ("title:" + norm_title(p["title"]))


def group(items, basis=None):
    """Bundle offers of the same product (same part number, else same title) across sites."""
    groups = {}
    for p in items:
        groups.setdefault(group_key(p), []).append(p)
    out = []
    for key, offers in groups.items():
        offers.sort(key=lambda p: (price_of(p, basis) is None, price_of(p, basis) or float("inf")))
        priced = [price_of(o, basis) for o in offers if price_of(o, basis) is not None]
        out.append({
            "key": key,
            "title": offers[0]["title"],
            "mpn": next((o["mpn"] for o in offers if o["mpn"]), ""),
            "image": next((o["image"] for o in offers if o["image"]), ""),
            "best_price": min(priced) if priced else None,
            "max_price": max(priced) if priced else None,
            "sites": len({o["site"] for o in offers}),
            "offers": offers,
        })
    # keep the order of the incoming (already sorted) list: by each group's first offer
    rank = {}
    for i, p in enumerate(items):
        rank.setdefault(group_key(p), i)
    out.sort(key=lambda g: rank[g["key"]])
    return out
