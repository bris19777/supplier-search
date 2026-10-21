from supsearch import filters
from supsearch.models import Product
from supsearch.normalize import extract_specs


def prod(site, title, price, stock=True, mpn="", brand=""):
    return Product(site=site, site_name=site.upper(), title=title, url=f"https://{site}/{title}",
                   mpn=mpn, brand=brand, price=price, price_inc_vat=price,
                   price_ex_vat=round(price / 1.18, 2) if price else None,
                   in_stock=stock, specs=extract_specs(title)).to_dict()


def sample():
    return [
        prod("a", "Lenovo laptop i5 16GB RAM 512GB SSD", 3500, True, "82RK00AB", "Lenovo"),
        prod("b", "Lenovo laptop i5 16GB RAM 512GB SSD", 3300, False, "82rk-00ab", "Lenovo"),
        prod("b", "HP laptop i7 32GB RAM 1TB SSD", 5200, True, "", "HP"),
        prod("c", "laptop bag", 90, True, "", ""),
        prod("c", "Dell laptop i5 8GB RAM 256GB SSD", None, None, "", "Dell"),
    ]


def test_facet_filter_and_counts_exclude_own_facet():
    f = {"facets": {"brand": ["Lenovo"]}}
    items, facets, _ = filters.apply(sample(), "laptop", f)
    assert {p["brand"] for p in items} == {"Lenovo"}
    brand = next(x for x in facets if x["key"] == "brand")
    counts = {v["value"]: v["count"] for v in brand["values"]}
    # other brands still counted while Lenovo is selected
    assert counts == {"Lenovo": 2, "HP": 1, "Dell": 1}
    assert next(v for v in brand["values"] if v["value"] == "Lenovo")["selected"]
    site = next(x for x in facets if x["key"] == "site")
    assert {v["value"]: v["count"] for v in site["values"]} == {"A": 1, "B": 1}


def test_price_stock_text_filters():
    items, _, rng = filters.apply(sample(), "laptop", {"price_min": 1000, "price_max": 4000, "in_stock": True})
    assert [p["site"] for p in items] == ["a"]
    assert rng == [90, 5200]
    items, _, _ = filters.apply(sample(), "laptop", {"exclude": "bag", "has_price": True})
    assert all("bag" not in p["title"] for p in items) and len(items) == 3
    items, _, _ = filters.apply(sample(), "laptop", {"include": "i5"})
    assert len(items) == 3


def test_relevance_filter_and_sorting():
    items, _, _ = filters.apply(sample(), "lenovo i5", {"min_relevance": 1})
    assert {p["brand"] for p in items} == {"Lenovo"}
    items, _, _ = filters.apply(sample(), "laptop", {"sort": "price_asc"})
    prices = [p["price_inc_vat"] for p in items]
    assert prices == [90, 3300, 3500, 5200, None]  # no price goes last
    items, _, _ = filters.apply(sample(), "laptop", {"sort": "price_desc"})
    assert [p["price_inc_vat"] for p in items][:2] == [5200, 3500]


def test_price_basis_ex_vat():
    items, _, _ = filters.apply(sample(), "laptop", {"basis": "ex", "price_max": 3000, "has_price": True})
    assert sorted(p["price_ex_vat"] for p in items) == [76.27, 2796.61, 2966.1]


def test_group_by_mpn_across_sites():
    items, _, _ = filters.apply(sample(), "laptop", {"sort": "price_asc"})
    groups = filters.group(items)
    lenovo = next(g for g in groups if g["key"] == "mpn:82RK00AB")
    assert lenovo["sites"] == 2 and lenovo["best_price"] == 3300 and lenovo["max_price"] == 3500
    assert [o["site"] for o in lenovo["offers"]] == ["b", "a"]
    assert len(groups) == 4


def specs_sample():
    return [
        prod("a", 'Laptop Core i7 16GB RAM 512GB SSD 14" RTX 4060', 5000),
        prod("a", 'Laptop Core i3 8GB RAM 256GB SSD 15.6"', 2000),
        prod("b", 'Laptop Ryzen 5 32GB RAM 1TB SSD 16" RTX 3050', 3500),
        prod("b", 'Laptop Core Ultra 7 16GB RAM 1TB SSD 13.3" RTX 4060Ti', 6000),
        prod("c", "Laptop bag", 90),
        prod("c", 'Laptop Core i5 8GB RAM 512GB SSD 15.6"', 2900),
        prod("c", 'Laptop Core i5 8GB RAM 512GB SSD 15.6"', 2700),
    ]


def _titles_cpu(items):
    return [p["specs"].get("cpu") for p in items]


def test_sort_by_cpu_tier_then_price_missing_last():
    items, _, _ = filters.apply(specs_sample(), "laptop", {"sort": "cpu_desc"})
    assert _titles_cpu(items) == ["Core Ultra 7", "Core i7", "Core i5", "Core i5", "Ryzen 5", "Core i3", None]
    # Core i5 and Ryzen 5 share a tier, so within it the cheapest comes first
    i5 = [p["price_inc_vat"] for p in items if p["specs"].get("cpu") == "Core i5"]
    assert i5 == [2700, 2900]  # cheapest first within the same CPU tier
    items, _, _ = filters.apply(specs_sample(), "laptop", {"sort": "cpu_asc"})
    assert _titles_cpu(items)[0] == "Core i3" and _titles_cpu(items)[-1] is None


def test_sort_by_screen_ram_storage_gpu():
    screens = lambda items: [p["specs"].get("screen") for p in items]
    items, _, _ = filters.apply(specs_sample(), "laptop", {"sort": "screen_asc"})
    assert screens(items) == ['13.3"', '14"', '15.6"', '15.6"', '15.6"', '16"', None]
    items, _, _ = filters.apply(specs_sample(), "laptop", {"sort": "storage_desc"})
    assert [p["specs"].get("storage") for p in items][:2] == ["1TB", "1TB"]
    items, _, _ = filters.apply(specs_sample(), "laptop", {"sort": "ram_desc"})
    assert items[0]["specs"]["ram"] == "32GB"
    items, _, _ = filters.apply(specs_sample(), "laptop", {"sort": "gpu_desc"})
    assert [p["specs"].get("gpu") for p in items][:3] == ["RTX 4060Ti", "RTX 4060", "RTX 3050"]


def test_cpu_facet_listed_by_tier():
    _, facets, _ = filters.apply(specs_sample(), "laptop", {})
    cpu = next(f for f in facets if f["key"] == "cpu")
    assert [v["value"] for v in cpu["values"]] == ["Core i3", "Core i5", "Ryzen 5", "Core i7", "Core Ultra 7"]


def test_ranks():
    assert filters.cpu_rank("Apple M3 Pro") > filters.cpu_rank("Apple M3")
    assert filters.cpu_rank("Something") is None
    assert filters.gpu_rank("RTX 4070") > filters.gpu_rank("RTX 3060") > filters.gpu_rank("GTX 1650")
    assert filters.gpu_rank("RX 7600XT") > filters.gpu_rank("RX 7600")
