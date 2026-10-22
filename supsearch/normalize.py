"""Turning the raw text scraped from each site into comparable values.

Prices, stock wording and part numbers look different on every site; the
functions here map them onto one shape so results can be filtered, sorted and
matched across sites. extract_specs() pulls the facet values (CPU, RAM, ...)
out of the product title, since most sites only expose specs inside it.
"""

import re

VAT_RATE = 0.18  # Israeli VAT since January 2025; overridable in sites.yaml

_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def parse_price(text):
    """'₪1,234.90' / '1,234 ש"ח' / 1234 -> 1234.9; None when there is no number."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text) if text > 0 else None
    m = _NUM_RE.search(str(text).replace("‏", "").replace("\xa0", " "))
    if not m:
        return None
    try:
        value = float(m.group(0).replace(",", ""))
    except ValueError:
        return None
    return value if value > 0 else None


def with_vat(price, vat_included, vat_rate=VAT_RATE):
    """Return (price_inc_vat, price_ex_vat) from a price and whether it includes VAT."""
    if price is None:
        return None, None
    if vat_included:
        return round(price, 2), round(price / (1 + vat_rate), 2)
    return round(price * (1 + vat_rate), 2), round(price, 2)


# Negative phrases first: "לא במלאי" contains "במלאי".
_OUT_OF_STOCK = ("אזל", "לא במלאי", "אין במלאי", "לא זמין", "חסר", "בהזמנה מיוחדת",
                 "אינו נמכר", "אינו מיוצר", "לא נמכר", "discontinued",
                 "out of stock", "sold out", "unavailable", "false", "outofstock")
_IN_STOCK = ("במלאי", "זמין", "קיים", "in stock", "available", "true", "instock",
             "הוסף", "הוספה לסל", "add to cart")


def parse_stock(text):
    """Stock wording/quantity -> True / False / None (unknown)."""
    if text is None:
        return None
    if isinstance(text, bool):
        return text
    if isinstance(text, (int, float)):
        return text > 0
    t = str(text).strip().lower()
    if not t:
        return None
    if re.fullmatch(r"\d+", t):
        return int(t) > 0
    for word in _OUT_OF_STOCK:
        if word in t:
            return False
    for word in _IN_STOCK:
        if word in t:
            return True
    return None


def norm_mpn(mpn):
    """Part numbers differ only in case/dashes/spaces between sites: '82RK-00AB iv' -> '82RK00ABIV'."""
    return re.sub(r"[^0-9A-Za-z]", "", mpn or "").upper()


def norm_title(title):
    t = (title or "").lower()
    t = re.sub(r"[^\w֐-׿]+", " ", t)
    return " ".join(t.split())


# Brands commonly carried by Israeli distributors. Matched as whole words in the title.
BRANDS = [
    "Lenovo", "HP", "Dell", "Asus", "Acer", "MSI", "Gigabyte", "Apple", "Samsung", "LG",
    "Microsoft", "Intel", "AMD", "Nvidia", "Kingston", "Crucial", "Western Digital", "WD",
    "Seagate", "Toshiba", "SanDisk", "ADATA", "Corsair", "G.Skill", "TeamGroup", "Team",
    "Patriot", "Lexar", "Transcend", "Logitech", "Razer", "SteelSeries", "HyperX",
    "Cooler Master", "Thermaltake", "NZXT", "be quiet!", "Fractal", "Deepcool", "Zalman",
    "Antec", "Seasonic", "TP-Link", "D-Link", "Netgear", "Ubiquiti", "Mikrotik", "Cisco",
    "Synology", "QNAP", "Epson", "Canon", "Brother", "Xerox", "Philips", "BenQ", "ViewSonic",
    "AOC", "Dahua", "Hikvision", "APC", "Eaton", "Sapphire", "PowerColor", "Zotac", "PNY",
    "Palit", "Inno3D", "Xiaomi", "Huawei", "Jabra", "Poly", "Genius", "Edifier", "Verbatim",
    "Arctic", "Noctua", "Ezviz", "Tenda", "Aruba", "Fortinet",
]
_BRAND_RE = [(b, re.compile(r"(?<![\w])" + re.escape(b) + r"(?![\w])", re.I)) for b in BRANDS]
_BRAND_ALIASES = {"WD": "Western Digital", "Team": "TeamGroup"}


def canonical_brand(name):
    """The site's own brand field, spelled one way across sites: 'LENOVO' -> 'Lenovo'."""
    name = " ".join((name or "").split())
    if not name:
        return ""
    known = detect_brand(name)
    if known:
        return known
    return name.title() if (name.isupper() or name.islower()) and len(name) > 3 else name


def detect_brand(title):
    for brand, rx in _BRAND_RE:
        if rx.search(title or ""):
            return _BRAND_ALIASES.get(brand, brand)
    return ""


def _size(num, unit):
    num = float(num)
    unit = unit.upper()
    if unit.startswith("T"):
        return f"{num:g}TB"
    return f"{num:g}GB"


_RAM_SIZES = {2, 4, 6, 8, 12, 16, 18, 24, 32, 36, 48, 64, 96, 128}


def _cpu(t):
    # Intel Core Ultra, also as distributors abbreviate it: "U7-355", "UX7-358H", "CORE U5-226V"
    m = re.search(r"\b(?:Core\s*)?Ultra\s*(X?)([3579])\b", t, re.I) or \
        re.search(r"\b(?:Core\s*)?U(X?)([3579])[\s-]+\d{3}", t, re.I)
    if m:
        return f"Core Ultra {m.group(1).upper()}{m.group(2)}"
    m = re.search(r"\b(?:Core\s*)?i([3579])\b", t, re.I)
    if m:
        return f"Core i{m.group(1)}"
    m = re.search(r"\bCore\s*([3579])\b", t, re.I) or \
        re.search(r"\bC([3579])-\d{3}", t)  # Intel Core 3/5/7 (Series 1/2, no "i"); "C5-210H"
    if m:
        return f"Core {m.group(1)}"
    m = re.search(r"\bRyzen[\s-]*(AI\s*)?([3579])\b", t, re.I) or \
        re.search(r"\b()R([3579])[\s-]+\d{4}", t) or \
        re.search(r"\bAMD\s+(AI\s+)?([3579])\s+(?:HX|PRO|\d{3})", t, re.I)
    if m:
        return "Ryzen " + ("AI " if m.group(1) else "") + m.group(2)
    # Apple silicon only with Apple context: a bare "M2" is usually "cd/m2" or "M.2"
    apple = re.search(r"apple|mac\s*book|imac|mac\s*mini|mac\s*studio", t, re.I)
    m = re.search(r"(?<![\w/.])M([1-5])(?:\s*(Pro|Max|Ultra))?\b", t)
    if m and (apple or m.group(2)):
        return f"Apple M{m.group(1)}" + (f" {m.group(2).title()}" if m.group(2) else "")
    m = re.search(r"\b(Celeron|Pentium|Athlon|Xeon|Snapdragon)\b", t, re.I)
    if m:
        return m.group(1).title()
    return None


def _is_monitor(t):
    return re.search(r"מסך|monitor", t, re.I) and \
        not re.search(r"כרטיס מסך|תומך|מתקן|זרוע|מגן|כבל|graphics", t, re.I)


def _screen(t):
    m = re.search(r"(?<![\d.])([1-8][0-9])(?:\.(\d))?\s*(?:\"|''|”|″|inch|אינץ'?|אינטש)", t, re.I)
    if m:
        return m.group(1) + ("." + m.group(2) if m.group(2) else "") + '"'
    # spec-list titles give a bare size between pipes: "U7-355|24G|512G|16|DOS"
    m = re.search(r"\|\s*(1[0-8])(?:\.(\d))?\s*\|", t)
    if m:
        return m.group(1) + ("." + m.group(2) if m.group(2) else "") + '"'
    if _is_monitor(t):
        # monitors give the size bare ("23.8") or only in the model ("S27-4e", "P40w-20");
        # a bare decimal is the most exact, then the model code, then a bare whole number
        bare = r"(?<![\w.-])(1[5-9]|[2-4][0-9]|57){}(?![\w.])"
        m = re.search(bare.format(r"\.(\d)"), t)
        if m:
            return f'{m.group(1)}.{m.group(2)}"'
        m = re.search(r"\b[A-Z]{1,3}(2[0-9]|3[0-9]|4[0-9])[a-z]?-\w", t) or re.search(bare.format(""), t)
        if m:
            return m.group(1) + '"'
    return None


def _gpu(t):
    m = re.search(r"\bRTX\s*(PRO)\s*(\d{3,4})\b", t, re.I)
    if m:
        return f"RTX PRO {m.group(2)}"
    m = re.search(r"\b(RTX|GTX|RX|Arc)\s*-?\s*(\d{3,4}\s*(?:Ti|XT|XTX|Super|GRE)?)\b", t, re.I)
    if m:
        return f"{m.group(1).upper()} {m.group(2).replace(' ', '')}"
    return None


def _os(t):
    m = re.search(r"\bW(?:in(?:dows)?)?\s*(1[01])\s*(Pro|Home|P|H)?(?![a-z])", t, re.I)
    if m:
        edition = {"P": " Pro", "PRO": " Pro", "H": " Home", "HOME": " Home"}.get((m.group(2) or "").upper(), "")
        return f"Windows {m.group(1)}{edition}"
    m = re.search(r"\b(FreeDOS|DOS|No\s*OS|Linux|Chrome\s*OS|macOS)\b", t, re.I) or \
        (re.search(r"\b(FD)\b", t) if _cpu(t) else None)  # "FD" = FreeDOS in computer titles
    if m:
        name = m.group(1)
        if name.upper().replace(" ", "") in ("DOS", "FREEDOS", "NOOS", "FD"):
            return "ללא מערכת הפעלה"
        return {"chromeos": "Chrome OS", "macos": "macOS"}.get(name.lower().replace(" ", ""), name.title())
    return None


def extract_specs(title):
    """Pull facet values out of a product title. Missing facets are simply absent."""
    t = title or ""
    specs = {}

    m = re.search(r"\b(\d{1,3})\s*GB\s*(?:DDR\d\w*|RAM|LPDDR\d\w*)\b", t, re.I) or \
        re.search(r"\b(?:RAM|DDR\d\w*|LPDDR\d\w*)\s*(\d{1,3})\s*GB\b", t, re.I)
    if m:
        specs["ram"] = f"{int(m.group(1))}GB"

    m = re.search(r"\b(\d+(?:\.\d+)?)\s*(TB|GB|T)\s*(?:NVMe\s*|M\.2\s*|PCIe\s*)*(SSD|HDD|NVMe|eMMC)\b", t, re.I) or \
        re.search(r"\b(SSD|HDD|NVMe)\s*(\d+(?:\.\d+)?)\s*(TB|GB|T|G)\b", t, re.I) or \
        re.search(r"\b(128|256|512)\s*(NVMe|SSD)\b", t, re.I)  # "512NVME": the unit left out
    if m:
        g = m.groups()
        if g[0].replace(".", "").isdigit():
            specs["storage"] = _size(g[0], g[1] if g[1].upper() in ("TB", "GB", "T") else "GB")
        else:
            specs["storage"] = _size(g[1], g[2])

    # Spec-list titles ("i5-14400T | 16GB | 512GB | Dos") give bare sizes: the first
    # common RAM size is the memory when something bigger (the storage) is listed too.
    found = re.findall(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(TB|GB)(?![a-z])", t, re.I)
    # "|24G|512G|": a unit without the B, trusted only as a pipe-delimited spec field
    found += re.findall(r"(?:^|\|)\s*(\d+)\s*(G|T)\s*(?=\||$)", t)
    if not found and _cpu(t):
        # a computer title written "i5-13420H 16G D5 512G": bare G/T words count too
        # ("4G LTE" / "5G" are modems, not memory)
        found = re.findall(r"(?<![\w.])(\d+)(G|T)(?![\w.])(?!\s*(?:LTE|5G|Wi))", t)
    sizes = [(float(n) * (1024 if u.upper().startswith("T") else 1), _size(n, u))
             for n, u in found]
    if "storage" not in specs and len(sizes) >= 2:
        gb, label = max(sizes)
        if gb >= 120:
            specs["storage"] = label
    if "ram" not in specs and (len(sizes) >= 2 or "storage" in specs):
        ram = next((label for gb, label in sizes if gb in _RAM_SIZES and label != specs.get("storage")), None)
        if ram:
            specs["ram"] = ram

    for key, fn in (("cpu", _cpu), ("screen", _screen), ("gpu", _gpu), ("os", _os)):
        value = fn(t)
        if value:
            specs[key] = value
    return specs
