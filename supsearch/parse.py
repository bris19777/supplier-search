"""Extracting products from a fetched search page, driven by a site's config.

Pure functions (no network), so a page saved from the real site can be tested
offline against its selectors.

Field specs, for format: html
  ".price"              text of the first match inside the item
  "a.title@href"        attribute of the first match
  "@data-id"            attribute of the item element itself
  "."                   text of the item element itself
  [spec, spec, ...]     first spec that yields a non-empty value
  {sel: spec, re: "..."}  then keep regex group 1 (or the whole match)
  {const: "value"}      a fixed value

For format: json, "item" and the field specs are dot paths into the JSON
("resources.results.products", "variants.0.price"), with the same list/re/const forms.
"""

import json
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .models import Product
from .normalize import (detect_brand, extract_specs, parse_price, parse_stock,
                        with_vat)

FIELDS = ("title", "url", "image", "sku", "mpn", "brand", "category", "price", "stock")
_ATTR_TAIL = re.compile(r"^(.*?)@([\w:-]+)$")


def _clean(text):
    return " ".join(str(text).split()) if text is not None else ""


def _html_value(item, spec):
    if spec in (".", ""):
        return _clean(item.get_text(" "))
    m = _ATTR_TAIL.match(spec)
    sel, attr = (m.group(1).strip(), m.group(2)) if m else (spec.strip(), None)
    node = item if not sel else item.select_one(sel)
    if node is None:
        return ""
    if attr:
        val = node.get(attr, "")
        if isinstance(val, list):
            val = " ".join(val)
        return _clean(val)
    return _clean(node.get_text(" "))


def _json_value(item, spec):
    cur = item
    for part in spec.split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return ""
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return ""
        if cur is None:
            return ""
    if isinstance(cur, (dict, list)):
        return ""
    return cur if isinstance(cur, (bool, int, float)) else _clean(cur)


def field_value(item, spec, getter):
    if spec is None:
        return ""
    if isinstance(spec, list):
        for s in spec:
            v = field_value(item, s, getter)
            if v not in ("", None):
                return v
        return ""
    if isinstance(spec, dict):
        if "const" in spec:
            return spec["const"]
        v = field_value(item, spec.get("sel", "."), getter)
        if spec.get("re") and v not in ("", None):
            m = re.search(spec["re"], str(v))
            if not m:
                return ""
            v = _clean(m.group(1) if m.groups() else m.group(0))
        return v
    return getter(item, spec)


def _items(content, site):
    fmt = site.get("format", "html")
    if fmt == "json":
        data = json.loads(content) if isinstance(content, (str, bytes)) else content
        cur = data
        for part in (site.get("item") or "").split("."):
            if not part:
                continue
            cur = cur.get(part) if isinstance(cur, dict) else None
            if cur is None:
                return [], _json_value
        return (cur if isinstance(cur, list) else []), _json_value
    soup = BeautifulSoup(content, "html.parser")
    return soup.select(site["item"]), _html_value


def parse_results(content, site, page_url, vat_rate=0.18, rates=None):
    """Return the list of Product found in one fetched page of `site`.

    `rates` maps a currency to shekels ({"USD": 3.09}); a site priced in another
    currency (`currency: USD`) gets its VAT prices converted so all sites compare."""
    items, getter = _items(content, site)
    fields = site.get("fields") or {}
    products = []
    seen = set()
    for item in items:
        raw = {f: field_value(item, fields.get(f), getter) for f in FIELDS}
        title = _clean(raw["title"])
        if not title:
            continue
        url = urljoin(page_url, str(raw["url"])) if raw["url"] else ""
        if url and url in seen:
            continue  # the same card can appear twice (grid + list markup)
        seen.add(url)
        price = parse_price(raw["price"])
        if price is not None and site.get("price_divisor"):
            price = price / site["price_divisor"]  # e.g. Shopify JSON prices in agorot
        currency = (site.get("currency") or "ILS").upper()
        rate = 1.0 if currency == "ILS" else (rates or {}).get(currency)
        ils = price * rate if price is not None and rate else None
        inc, ex = with_vat(ils, site.get("vat_included", True), vat_rate)
        stock_raw = raw["stock"]
        in_stock = parse_stock(stock_raw)
        if in_stock is None and site.get("stock_default") is not None:
            in_stock = bool(site["stock_default"])
        image = str(raw["image"] or "")
        if image and not image.startswith("data:"):
            image = urljoin(page_url, image.split(",")[0].split(" ")[0])
        else:
            image = ""
        brand = _clean(raw["brand"]) or detect_brand(title)
        products.append(Product(
            site=site["id"], site_name=site["name"], title=title, url=url,
            image=image, sku=_clean(raw["sku"]), mpn=_clean(raw["mpn"]),
            brand=brand, category=_clean(raw["category"]),
            price=price, currency=currency, price_inc_vat=inc, price_ex_vat=ex,
            in_stock=in_stock, stock_text=_clean(stock_raw) if not isinstance(stock_raw, bool) else "",
            specs=extract_specs(title),
        ))
    return products
