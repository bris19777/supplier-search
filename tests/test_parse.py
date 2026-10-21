import json

from supsearch.parse import parse_results

HTML = """
<div class="grid">
  <div class="card" data-sku="A100">
    <a class="name" href="/p/1">Lenovo IdeaPad i5 16GB RAM 512GB SSD</a>
    <img data-src="/img/1.jpg" src="data:image/gif;base64,xx">
    <span class="old">₪3,999</span><span class="now">₪3,499</span>
    <div class="desc">מק"ט יצרן: 82RK00ABIV</div>
    <button class="add">הוסף לסל</button>
  </div>
  <div class="card" data-sku="A200">
    <a class="name" href="https://shop.test/p/2">WD Blue 1TB SSD</a>
    <img src="/img/2.jpg">
    <span class="now">299</span>
    <span class="stock">אזל מהמלאי</span>
  </div>
  <div class="card"><span class="now">5</span></div>
</div>
"""

SITE = {
    "id": "shop", "name": "חנות", "item": ".card", "vat_included": False,
    "fields": {
        "title": "a.name",
        "url": "a.name@href",
        "image": ["img@data-src", "img@src"],
        "sku": "@data-sku",
        "mpn": {"sel": ".desc", "re": r'מק"ט יצרן:\s*(\S+)'},
        "price": ".now",
        "stock": [".stock", {"sel": "button.add", "re": "הוסף"}],
        "category": {"const": "מחשבים"},
    },
}


def test_parse_html():
    items = parse_results(HTML, SITE, "https://shop.test/search?q=x", vat_rate=0.18)
    assert len(items) == 2  # the card without a title is skipped
    a, b = items
    assert a.url == "https://shop.test/p/1"
    assert a.image == "https://shop.test/img/1.jpg"
    assert a.sku == "A100" and a.mpn == "82RK00ABIV"
    assert a.price == 3499 and a.price_ex_vat == 3499 and a.price_inc_vat == 4128.82
    assert a.in_stock is True
    assert a.brand == "Lenovo" and a.category == "מחשבים"
    assert a.specs["ram"] == "16GB" and a.specs["storage"] == "512GB"
    assert b.in_stock is False and b.brand == "Western Digital" and b.image.endswith("/img/2.jpg")


def test_parse_json():
    site = {
        "id": "j", "name": "J", "format": "json", "item": "resources.results.products",
        "price_divisor": 100,
        "fields": {"title": "title", "url": "url", "price": "price", "stock": "available",
                   "brand": "vendor", "sku": "variants.0.sku"},
    }
    data = {"resources": {"results": {"products": [
        {"title": "Samsung 990 Pro 2TB NVMe", "url": "/products/x", "price": 89900,
         "available": False, "vendor": "Samsung", "variants": [{"sku": "MZ-V9P2T0"}]},
    ]}}}
    [p] = parse_results(json.dumps(data), site, "https://j.test/search/suggest.json")
    assert p.price == 899 and p.in_stock is False and p.sku == "MZ-V9P2T0"
    assert p.url == "https://j.test/products/x" and p.brand == "Samsung"


def test_parse_json_missing_path():
    site = {"id": "j", "name": "J", "format": "json", "item": "nope.items", "fields": {"title": "t"}}
    assert parse_results("{}", site, "https://j.test/") == []


def test_foreign_currency_converted_to_shekels():
    site = {"id": "u", "name": "U", "item": ".card", "currency": "usd", "vat_included": False,
            "fields": {"title": "a.name", "price": ".now"}}
    html = '<div class="card"><a class="name">Asus V500</a><span class="now">$100</span></div>'
    [p] = parse_results(html, site, "https://u.test/", vat_rate=0.18, rates={"USD": 3.1})
    assert p.price == 100 and p.currency == "USD"
    assert p.price_ex_vat == 310.0 and p.price_inc_vat == 365.8
    [p] = parse_results(html, site, "https://u.test/", rates={})  # rate unknown: no shekel price
    assert p.price == 100 and p.price_inc_vat is None
