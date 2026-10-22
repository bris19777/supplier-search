"""Deep search: reading specs from a product's own page when its title lacks them.

Sites describe the product page in sites.yaml under `detail`:
  rows: CSS selector of spec rows ("label | value"); the first child element is
        the label, the rest the value (or "label: value" in one text)
  text: CSS selector of a free-text description, scanned like a title

A row counts only when its label names the spec AND its value parses as one, so
"Max memory: up to 64GB" or "Memory slots: 2" never pass for the installed RAM.
"""

import re

from bs4 import BeautifulSoup

from .normalize import _cpu, _gpu, _os, _screen, _size, extract_specs

# facet -> (label words, label words that disqualify the row)
_LABELS = {
    "cpu": (("מעבד", "processor", "cpu"), ("דור", "ליבות", "תדר", "מטמון", "cache", "generation",
                                           "cores", "threads", "frequency", "קירור")),
    "ram": (("זיכרון", "זכרון", "ram", "memory"), ("מקסימ", "max", "חריצ", "slot", "גרפי", "כרטיס",
                                                   "מטמון", "cache", "פנוי", "סוג", "type", "מהירות", "speed")),
    "storage": (("אחסון", "דיסק", "כונן", "storage", "ssd", "hdd", "hard drive"),
                ("חיצוני", "קורא", "reader", "חריצ", "slot", "תמיכה", "support", "נוסף")),
    "screen": (("מסך", "תצוגה", "display", "screen"), ("יציאת", "יציאות", "port", "hdmi", "רזולוציה",
                                                         "resolution", "קצב", "refresh", "טכנולוג",
                                                         "סוג", "type", "בהירות", "brightness", "מגע", "touch",
                                                         "כרטיס", "ציפוי")),
    "gpu": (("כרטיס מסך", "כרטיס גרפי", "גרפיקה", "graphics", "gpu", "מאיץ"), ()),
    "os": (("מערכת הפעלה", "operating system"), ("תמיכה", "support", "תואם", "compatible")),
}


def _size_value(value):
    m = re.search(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(TB|GB)(?![a-z])", value, re.I)
    return _size(m.group(1), m.group(2)) if m else None


def _screen_value(value):
    found = _screen(value)
    if found:
        return found
    m = re.search(r"(?<![\w.])(1[0-9]|[2-8][0-9])(?:\.(\d))?(?![\w.])", value)
    return (m.group(1) + ("." + m.group(2) if m.group(2) else "") + '"') if m else None


_VALUE_PARSERS = {
    "cpu": _cpu,
    "ram": lambda v: (_size_value(v) if _size_value(v) and not _size_value(v).endswith("TB") else None),
    "storage": _size_value,
    "screen": _screen_value,
    "gpu": _gpu,
    "os": _os,
}


def _label_facet(label):
    label = label.lower()
    for facet, (words, excluded) in _LABELS.items():
        if any(w in label for w in words) and not any(x in label for x in excluded):
            return facet
    return None


def _row_pairs(row):
    parts = [c.get_text(" ", strip=True) for c in row.find_all(recursive=False)]
    parts = [p for p in parts if p]
    if len(parts) >= 2:
        return parts[0], " ".join(parts[1:])
    text = row.get_text(" ", strip=True)
    if len(parts) == 1 and text.startswith(parts[0]) and len(text) > len(parts[0]):
        return parts[0], text[len(parts[0]):]  # "<span>label</span> value"
    if ":" in text:
        label, _, value = text.partition(":")
        return label, value
    return None


def parse_detail(html, detail):
    """Specs found on a product page, by the site's `detail` config."""
    soup = BeautifulSoup(html, "html.parser")
    specs = {}
    if detail.get("rows"):
        for row in soup.select(detail["rows"]):
            pair = _row_pairs(row)
            if not pair:
                continue
            facet = _label_facet(pair[0])
            if facet and facet not in specs:
                value = _VALUE_PARSERS[facet](pair[1])
                if value:
                    specs[facet] = value
    if detail.get("text"):
        node = soup.select_one(detail["text"])
        if node is not None:
            for key, value in extract_specs(node.get_text(" ", strip=True)).items():
                specs.setdefault(key, value)
    return specs


def all_rows(html, detail):
    """Every "label | value" row of the product page's spec table, for a full spec sheet."""
    if not detail.get("rows"):
        return []
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for row in soup.select(detail["rows"]):
        pair = _row_pairs(row)
        if pair and pair[0].strip() and pair[1].strip():
            out.append({"label": " ".join(pair[0].split())[:80], "value": " ".join(pair[1].split())[:300]})
    return out[:60]


# Products worth opening: computers and monitors whose title is missing a spec
_COMPUTER = re.compile(r"מחשב|נייד|נייח|laptop|notebook|desktop|aio|all.in.one|thinkpad|ideapad|"
                       r"thinkcentre|thinkbook|vivobook|zenbook|probook|elitebook|latitude|vostro|"
                       r"inspiron|legion|loq|yoga|macbook|imac|mini pc|workstation|תחנת עבודה", re.I)
_MONITOR = re.compile(r"מסך|monitor", re.I)
_ACCESSORY = re.compile(r"תיק|מטען|שנאי|כבל|מתאם|תומך|זרוע|מגן|נרתיק|סוללה|הרחבת אחריות|מקלדת|עכבר|"
                        r"bag|backpack|sleeve|charger|adapter|cable|warranty|dock|תחנת עגינה|כרטיס מסך", re.I)
COMPUTER_SPECS = ("cpu", "ram", "storage")


def wants_detail(product):
    """Would opening this product's page likely fill a missing spec?"""
    title = product.title
    if _ACCESSORY.search(title):
        return False
    if _COMPUTER.search(title):
        return any(k not in product.specs for k in COMPUTER_SPECS)
    if _MONITOR.search(title):
        return "screen" not in product.specs
    return False
