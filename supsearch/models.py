"""Data shapes shared by the sources, the aggregator and the web API."""

from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class Product:
    site: str                      # site id from sites.yaml
    site_name: str                 # display name
    title: str
    url: str = ""
    image: str = ""
    sku: str = ""                  # the site's own catalog number
    mpn: str = ""                  # manufacturer part number, used to match across sites
    brand: str = ""
    category: str = ""
    price: Optional[float] = None  # as shown on the site, in `currency`
    currency: str = "ILS"
    price_inc_vat: Optional[float] = None  # in shekels, for comparing across sites
    price_ex_vat: Optional[float] = None
    in_stock: Optional[bool] = None  # None = the site did not say
    stock_text: str = ""
    specs: dict = field(default_factory=dict)  # facet key -> value, see normalize.extract_specs
    deep_specs: list = field(default_factory=list)  # spec keys read from the product page (deep search)

    def to_dict(self):
        return asdict(self)


@dataclass
class SiteResult:
    site: str
    site_name: str
    ok: bool
    items: list = field(default_factory=list)
    error: str = ""
    needs_login: bool = False
    elapsed: float = 0.0
    cached: bool = False
    from_index: bool = False  # answered from the local index, not the site
    index_age: float = 0.0    # seconds since the oldest of these products was last seen
    out_of_stock: list = field(default_factory=list)  # dropped: marked out of stock by the site
    deep_read: int = 0       # product pages opened by deep search
    deep_filled: int = 0     # products that gained specs from them

    def summary(self):
        return {
            "site": self.site, "site_name": self.site_name, "ok": self.ok,
            "count": len(self.items), "error": self.error,
            "needs_login": self.needs_login, "elapsed": round(self.elapsed, 2),
            "cached": self.cached, "from_index": self.from_index,
            "index_age": round(self.index_age), "out_of_stock": len(self.out_of_stock),
            "deep_read": self.deep_read, "deep_filled": self.deep_filled,
        }
