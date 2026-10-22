"""Local index: full-text lookup, the index-first search path, and the crawl."""

import asyncio
import time

import yaml
from fastapi.testclient import TestClient

from supsearch.index import Crawler, Index, find, fts_query
from supsearch.models import Product
from supsearch.normalize import extract_specs
from test_e2e import server  # noqa: F401  (mock supplier sites fixture)


def prod(site, title, mpn="", url=None, **kw):
    return Product(site=site, site_name=site.upper(), title=title, mpn=mpn,
                   url=url or f"https://{site}/{abs(hash(title))}", specs=extract_specs(title), **kw).to_dict()


def titles(hits):
    return sorted(p["title"] for rows in hits.values() for p, _ in rows)


def test_fts_query_escapes():
    assert fts_query('lenovo "i5"') == '"lenovo"* "i5"*'
    assert fts_query("  ") == ""


def test_index_search(tmp_path):
    ix = Index(str(tmp_path / "ix.sqlite"))
    ix.upsert([
        prod("a", "מחשב נייד Lenovo ThinkPad L16 i5 16GB", mpn="21X8002RIV"),
        prod("a", "Kingston SSD 1TB NV3", mpn="SNV3S/1000G"),
        prod("b", "מסך Samsung 27 אינץ'"),
    ])
    assert titles(ix.search("lenovo")) == ["מחשב נייד Lenovo ThinkPad L16 i5 16GB"]
    assert titles(ix.search("lenov thinkp")) == ["מחשב נייד Lenovo ThinkPad L16 i5 16GB"]  # prefixes
    assert titles(ix.search("נייד")) == ["מחשב נייד Lenovo ThinkPad L16 i5 16GB"]          # Hebrew
    assert titles(ix.search("21x8002riv")) == ["מחשב נייד Lenovo ThinkPad L16 i5 16GB"]
    # a part number matches with or without its separators
    assert titles(ix.search("SNV3S1000G")) == titles(ix.search("SNV3S/1000G")) == ["Kingston SSD 1TB NV3"]
    assert ix.search("lenovo samsung") == {}                                          # every word must match
    assert list(ix.search("מסך", sites={"a"})) == []
    assert set(ix.stats()) == {"a", "b"} and ix.stats()["a"]["products"] == 2


def test_upsert_keeps_page_specs_and_age(tmp_path):
    ix = Index(str(tmp_path / "ix.sqlite"))
    p = prod("v", "מחשב נייד Lenovo IdeaPad 83US005QIV", url="https://v/1")
    ix.upsert([{**p, "specs": {**p["specs"], "cpu": "Core i5"}, "deep_specs": ["cpu"]}])
    seen = ix.stats()["v"]["newest"]
    time.sleep(0.01)
    ix.upsert([{**p, "price": 1.0}], touch=False)        # specs saved, not a new sighting
    [(row, last_seen)] = ix.search("ideapad")["v"]
    assert row["specs"]["cpu"] == "Core i5" and row["deep_specs"] == ["cpu"] and row["price"] == 1.0
    assert last_seen == seen
    ix.upsert([p])                                        # a real sighting refreshes the time
    assert ix.search("ideapad")["v"][0][1] > seen
    assert ix.search("ideapad", max_age_days=1)
    assert ix.drop_site("v") == 1 and ix.search("ideapad") == {}


def test_prune(tmp_path):
    ix = Index(str(tmp_path / "ix.sqlite"))
    ix.upsert([prod("a", "old thing")])
    ix._db.execute("UPDATE products SET last_seen = ?", (time.time() - 20 * 86400,))
    assert ix.search("old", max_age_days=3) == {}         # too old to answer with
    assert ix.prune(14) == 1 and ix.stats() == {}


# -- the search path and the crawl, against the mock shop --------------------------

def make_agg(server_url, tmp_path):
    from supsearch.aggregator import Aggregator
    from supsearch.store import Credentials, Store
    sites = [{"id": "a", "name": "A", "enabled": True, "fetch": "http", "format": "html", "pages": 1,
              "vat_included": True, "search_url": server_url + "/a/search?q={query}", "item": "div.p",
              "fields": {"title": "a", "url": "a@href", "price": ".price", "mpn": ".mpn"}}]
    return Aggregator({"timeout": 10, "cache_minutes": 0, "index": {}}, sites,
                      Credentials(Store(str(tmp_path / "data"))))


class Counting:
    """Wraps the aggregator's search to count live searches."""

    def __init__(self, agg):
        self.calls = []
        self._search = agg.search

        async def search(query, only=None, refresh=False):
            self.calls.append(query)
            return await self._search(query, only=only, refresh=refresh)
        agg.search = search


def test_find_index_first(tmp_path, server):  # noqa: F811
    async def go():
        agg = make_agg(server, tmp_path)
        live = Counting(agg)
        ix = Index(str(tmp_path / "ix.sqlite"))
        r = await find(agg, ix, "kingston", source="auto")         # empty index: live, then indexed
        assert live.calls == ["kingston"] and not r[0].from_index and len(r[0].items) == 1
        r = await find(agg, ix, "kingston", source="auto")         # now straight from the index
        assert live.calls == ["kingston"] and r[0].from_index and r[0].items[0].mpn == "SNV3S/1000G"
        r = await find(agg, ix, "samsung", source="auto")          # not indexed yet -> live
        assert live.calls == ["kingston", "samsung"]
        r = await find(agg, ix, "nothing-like-this", source="index")
        assert r[0].from_index and r[0].items == []                # index only: an empty answer
        await find(agg, ix, "kingston", refresh=True)              # a full refresh always goes live
        assert live.calls[-1] == "kingston"
        await agg.close()
    asyncio.run(go())


def test_crawler(tmp_path, server):  # noqa: F811
    async def go():
        agg = make_agg(server, tmp_path)
        ix = Index(str(tmp_path / "ix.sqlite"))
        crawler = Crawler(agg, ix, {"queries": ["kingston", "samsung", "nothing"], "pause_seconds": 0})
        assert await crawler.run() is True
        st = crawler.status
        assert not st["running"] and st["finished"] and st["last_added"] == 2
        assert st["progress"]["a"] == {"done": 3, "total": 3, "found": 2}
        assert titles(ix.search("ssd")) == ["Kingston SSD 1TB NV3 NVMe", "Samsung SSD 2TB 990 Pro"]
        await agg.close()
    asyncio.run(go())


def test_index_api(tmp_path, monkeypatch, server):  # noqa: F811
    from supsearch import config, fetch
    from supsearch.app import create_app
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(fetch, "DATA_DIR", str(tmp_path / "data"))
    sites = {"settings": {"channel": ""}, "index": {"queries": ["kingston", "samsung"], "pause_seconds": 0},
             "sites": [{"id": "a", "name": "חנות א", "fetch": "http", "search_url": server + "/a/search?q={query}",
                        "item": "div.p", "fields": {"title": "a", "url": "a@href", "price": ".price"}}]}
    (tmp_path / "sites.yaml").write_text(yaml.safe_dump(sites, allow_unicode=True), encoding="utf-8")
    with TestClient(create_app(str(tmp_path / "sites.yaml"), str(tmp_path / "none.yaml")),
                    client=("127.0.0.1", 50000)) as c:
        assert c.get("/api/admin/index").json()["sites"][0]["products"] == 0
        assert c.post("/api/admin/index/run").json()["ok"]
        for _ in range(100):
            st = c.get("/api/admin/index").json()
            if st["status"]["finished"]:
                break
            time.sleep(0.1)
        assert st["sites"][0]["products"] == 2 and st["queries"] == 2
        j = c.post("/api/search", json={"query": "samsung"}).json()
        assert j["sites"][0]["from_index"] and j["items"][0]["title"] == "Samsung SSD 2TB 990 Pro"
        j = c.post("/api/search", json={"query": "samsung", "source": "live"}).json()
        assert not j["sites"][0]["from_index"] and j["total"] == 1


def test_out_of_stock_never_kept(tmp_path):
    path = str(tmp_path / "ix.sqlite")
    ix = Index(path)
    p = prod("a", "Kingston SSD 1TB NV3", url="https://a/1", in_stock=True)
    ix.upsert([p])
    assert ix.search("kingston")
    ix.upsert([{**p, "in_stock": False}])                 # it ran out: gone from the index
    assert ix.search("kingston") == {} and ix.stats() == {}
    ix.upsert([prod("a", "Samsung SSD", url="https://a/2", in_stock=False)])
    assert ix.stats() == {}                               # never added in the first place
    # rows written before this rule existed are cleaned when the index opens
    ix._db.execute("INSERT INTO products(key, site, data, last_seen) VALUES ('a|x', 'a', ?, ?)",
                   ('{"title": "old", "in_stock": false}', time.time()))
    ix._db.commit()
    ix.close()
    assert Index(path).stats() == {}


def test_out_of_stock_dropped_at_the_source(tmp_path, server):  # noqa: F811
    async def go():
        agg = make_agg(server, tmp_path)
        agg.sites[0]["fields"]["stock"] = ".st"         # the mock shop marks Samsung "אזל"
        ix = Index(str(tmp_path / "ix.sqlite"))
        crawler = Crawler(agg, ix, {"queries": ["ssd"], "pause_seconds": 0})
        await crawler.run()
        assert titles(ix.search("ssd")) == ["Kingston SSD 1TB NV3 NVMe"]
        [res] = await find(agg, ix, "samsung", source="live")
        assert res.items == [] and len(res.out_of_stock) == 1
        assert ix.search("samsung") == {}
        await agg.close()
    asyncio.run(go())


def test_index_results_use_todays_rate(tmp_path, server):  # noqa: F811
    async def go():
        agg = make_agg(server, tmp_path)
        agg.sites[0]["vat_included"] = False
        ix = Index(str(tmp_path / "ix.sqlite"))
        p = prod("a", "Lenovo ThinkPad dollar priced", url="https://a/9", price=1000.0, currency="USD",
                 price_ex_vat=3000.0, price_inc_vat=3540.0)   # converted days ago at 3.0
        ix.upsert([p])
        agg._rates["USD"] = (time.time(), 3.7, "2026-10-22", "בנק ישראל")  # today's rate
        results = await find(agg, ix, "thinkpad")
        used = await agg.apply_rates(results)
        item = results[0].items[0]
        assert item.price_ex_vat == 3700.0 and item.price_inc_vat == 4366.0
        assert used == {"USD": {"rate": 3.7, "date": "2026-10-22", "source": "בנק ישראל"}}
        await agg.close()
    asyncio.run(go())
