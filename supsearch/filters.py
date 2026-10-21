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


# Rough performance tiers, so "sort by CPU" puts a Core i3 below an i5 and an i7,
# with the matching AMD/Apple parts beside them.
_CPU_TIERS = {
    "Celeron": 1, "Athlon": 2, "Pentium": 2, "Snapdragon": 5,
    "Core i3": 3, "Ryzen 3": 3, "Core 3": 3.5,
    "Core i5": 5, "Ryzen 5": 5, "Apple M1": 5, "Core 5": 5.5,
    "Core Ultra 5": 6, "Ryzen AI 5": 6, "Apple M2": 6, "Core Ultra X5": 6.5,
    "Core i7": 7, "Ryzen 7": 7, "Apple M3": 7, "Core 7": 7.5,
    "Core Ultra 7": 8, "Ryzen AI 7": 8, "Apple M4": 8, "Core Ultra X7": 8.5,
    "Core i9": 9, "Ryzen 9": 9, "Xeon": 9, "Apple M5": 9,
    "Core Ultra 9": 10, "Ryzen AI 9": 10, "Core Ultra X9": 10.5,
}


def cpu_rank(value):
    if value in _CPU_TIERS:
        return _CPU_TIERS[value]
    base = re.sub(r"\s*(Pro|Max|Ultra)$", "", value, flags=re.I)  # "M3 Pro" just above "M3"
    return _CPU_TIERS[base] + 0.5 if base in _CPU_TIERS else None


# Workstation RTX (RTX 500 / 1000 / 2000 ... Ada, RTX PRO 4500 ...) roughly next to the
# consumer card of similar class, as a tier on the same xx50..xx90 scale.
_PRO_GPU_TIERS = {500: 45, 1000: 50, 2000: 60, 3000: 70, 3500: 72, 4000: 75,
                  4500: 78, 5000: 80, 6000: 90}


def gpu_rank(value):
    """'RTX 4060Ti' -> tier 60 first, then generation 40, then the Ti/Super/XT step."""
    m = re.match(r"(\w+)\s+(?:PRO\s+)?(\d{3,4})(\w*)", value)
    if not m:
        return None
    digits = m.group(2)
    if m.group(1) == "RTX" and int(digits) in _PRO_GPU_TIERS:
        return _PRO_GPU_TIERS[int(digits)] * 100 + 45
    tier, gen = int(digits[-2:]), int(digits[:-2])
    bump = 5 if m.group(3).upper() in ("TI", "SUPER", "XT", "XTX", "GRE") else 0
    return tier * 100 + gen + bump


def _size_rank(value):
    k = _num_key(value)
    return k[1] if k[0] == 0 else None


# sort key -> (facet that holds the value, value -> comparable number or None)
SPEC_SORTS = {
    "cpu": ("cpu", cpu_rank),
    "screen": ("screen", _size_rank),
    "ram": ("ram", _size_rank),
    "storage": ("storage", _size_rank),
    "gpu": ("gpu", gpu_rank),
}


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
        if key in SPEC_SORTS:
            # weakest/smallest first, so the list reads like a spec ladder
            rank = SPEC_SORTS[key][1]
            order = sorted(counts, key=lambda v: (rank(v) is None, rank(v) or 0, v))
        else:
            order = sorted(counts, key=lambda v: (-counts[v], v))
        facets.append({"key": key, "label": label, "values": [
            {"value": v, "count": counts[v], "selected": v in selected} for v in order]})

    basis = f.get("basis")
    sort = f.get("sort") or "relevance"
    no_price = float("inf")
    spec, _, direction = sort.rpartition("_")
    if spec in SPEC_SORTS:
        # "cpu_asc" / "screen_desc" ...: by the spec, cheapest first within the same
        # spec; products whose title doesn't state it go last either way
        facet, rank = SPEC_SORTS[spec]
        sign = -1 if direction == "desc" else 1

        def spec_key(p):
            r = rank(p["specs"].get(facet, ""))
            return (r is None, sign * (r or 0), price_of(p, basis) is None,
                    price_of(p, basis) or no_price)
        items.sort(key=spec_key)
    elif sort == "price_asc":
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
