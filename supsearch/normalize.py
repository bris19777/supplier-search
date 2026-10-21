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


def extract_specs(title):
    """Pull facet values out of a product title. Missing facets are simply absent."""
    t = title or ""
    specs = {}

    m = re.search(r"\b(?:Core\s*)?(?:(Ultra)\s*([3579])|i([3579]))\b", t, re.I)
    if m:
        specs["cpu"] = f"Core Ultra {m.group(2)}" if m.group(1) else f"Core i{m.group(3)}"
    else:
        m = re.search(r"\bRyzen\s*(AI\s*)?([3579])\b", t, re.I)
        if m:
            specs["cpu"] = "Ryzen " + ("AI " if m.group(1) else "") + m.group(2)
        else:
            m = re.search(r"\b(Celeron|Pentium|Athlon|Xeon|Snapdragon|Apple\s*M[1-5]|M[1-5]\s*(?:Pro|Max)?)\b", t, re.I)
            if m:
                specs["cpu"] = m.group(1).title()

    m = re.search(r"\b(\d{1,3})\s*GB\s*(?:DDR\d\w*|RAM|LPDDR\d\w*)\b", t, re.I) or \
        re.search(r"\b(?:RAM|DDR\d\w*|LPDDR\d\w*)\s*(\d{1,3})\s*GB\b", t, re.I)
    if m:
        specs["ram"] = f"{int(m.group(1))}GB"

    m = re.search(r"\b(\d+(?:\.\d+)?)\s*(TB|GB|T)\s*(?:NVMe\s*|M\.2\s*|PCIe\s*)*(SSD|HDD|NVMe|eMMC)\b", t, re.I) or \
        re.search(r"\b(SSD|HDD|NVMe)\s*(\d+(?:\.\d+)?)\s*(TB|GB|T)\b", t, re.I)
    if m:
        g = m.groups()
        if g[0].replace(".", "").isdigit():
            specs["storage"] = _size(g[0], g[1])
        else:
            specs["storage"] = _size(g[1], g[2])

    # Spec-list titles ("i5-14400T | 16GB | 512GB | Dos") give bare sizes: with two or
    # more sizes, the first common RAM size is the memory and the largest is the storage.
    sizes = [(float(n) * (1024 if u.upper() == "TB" else 1), _size(n, u))
             for n, u in re.findall(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(TB|GB)(?![a-z])", t, re.I)]
    if len(sizes) >= 2:
        if "ram" not in specs:
            ram = next((label for gb, label in sizes if gb in _RAM_SIZES), None)
            if ram and max(gb for gb, _ in sizes) > float(ram[:-2]):
                specs["ram"] = ram
        if "storage" not in specs:
            gb, label = max(sizes)
            if gb >= 120:
                specs["storage"] = label

    m = re.search(r"(?<![\d.])(1[0-9]|2[0-9]|3[0-9]|4[0-9]|5[0-9]|6[0-9]|7[0-9]|8[0-9])(?:\.(\d))?\s*(?:\"|''|”|inch|אינץ'?|אינטש)", t, re.I)
    if m:
        specs["screen"] = m.group(1) + ("." + m.group(2) if m.group(2) else "") + '"'

    m = re.search(r"\b(RTX|GTX|RX|Arc)\s*-?\s*(\d{3,4}\s*(?:Ti|XT|XTX|Super|GRE)?)\b", t, re.I)
    if m:
        specs["gpu"] = f"{m.group(1).upper()} {m.group(2).replace(' ', '')}"

    m = re.search(r"\bW(1[01])\s*(P|H|Pro|Home)?\b", t)
    if m:
        edition = {"P": " Pro", "Pro": " Pro", "H": " Home", "Home": " Home"}.get(m.group(2) or "", "")
        specs["os"] = f"Windows {m.group(1)}{edition}"
    m = None if "os" in specs else re.search(r"\b(Windows\s*1[01]\s*(?:Pro|Home)?|Win\s*1[01]\s*(?:Pro|Home)?|FreeDOS|DOS|No\s*OS|Linux|Chrome\s*OS|macOS)\b", t, re.I)
    if m:
        os_name = m.group(1)
        os_name = re.sub(r"^Win\s*", "Windows ", os_name, flags=re.I)
        os_name = re.sub(r"\s+", " ", os_name).strip()
        if os_name.upper() in ("DOS", "FREEDOS", "NO OS"):
            os_name = "ללא מערכת הפעלה"
        specs["os"] = os_name.title() if os_name[0].isascii() else os_name

    return specs
