"""Hebrew spec sheets for quotes: a built-in dictionary for the common labels and words
(always, free), refined by the AI when the organisation's key is set (see ai.translate_specs).

Model names, part numbers and units stay as they are ("Intel Core Ultra 7 265T", "DDR5").
"""

import re

# English labels (lower-case, as the suppliers / manufacturers write them) -> Hebrew
LABELS = {
    "processor": "מעבד", "cpu": "מעבד", "cputype": "סוג מעבד", "cpu type": "סוג מעבד",
    "processor model": "דגם מעבד", "cores": "ליבות", "memory": "זיכרון", "ram": "זיכרון",
    "max memory": "זיכרון מקסימלי", "memory slots": "חריצי זיכרון", "storage": "אחסון",
    "hard drive": "דיסק קשיח", "ssd": "כונן SSD", "display": "מסך", "screen": "מסך",
    "screen size": "גודל מסך", "resolution": "רזולוציה", "touchscreen": "מסך מגע", "touch": "מסך מגע",
    "graphics": "כרטיס גרפי", "gpu": "כרטיס גרפי", "video": "כרטיס גרפי",
    "operating system": "מערכת הפעלה", "os": "מערכת הפעלה", "chipset": "שבב",
    "wlan": "רשת אלחוטית", "wireless": "רשת אלחוטית", "wi-fi": "רשת אלחוטית", "wifi": "רשת אלחוטית",
    "ethernet": "רשת קווית", "lan": "רשת קווית", "bluetooth": "בלוטות'", "ports": "יציאות",
    "connectors": "חיבורים", "connectivity": "קישוריות", "camera": "מצלמה", "webcam": "מצלמה",
    "audio": "שמע", "speakers": "רמקולים", "microphone": "מיקרופון", "keyboard": "מקלדת",
    "battery": "סוללה", "power supply": "ספק כוח", "power adapter": "ספק כוח", "adapter": "ספק כוח",
    "weight": "משקל", "dimensions": "מידות", "color": "צבע", "colour": "צבע", "warranty": "אחריות",
    "form factor": "סוג מארז", "case": "מארז", "security": "אבטחה", "fingerprint reader": "קורא טביעות אצבע",
    "optical drive": "כונן אופטי", "card reader": "קורא כרטיסים", "model": "דגם", "part number": "מק\"ט",
    "brand": "יצרן", "manufacturer": "יצרן", "refresh rate": "קצב רענון", "response time": "זמן תגובה",
    "panel": "סוג פאנל", "panel type": "סוג פאנל", "brightness": "בהירות", "contrast": "ניגודיות",
    "interface": "ממשק", "capacity": "נפח", "speed": "מהירות", "read speed": "מהירות קריאה",
    "write speed": "מהירות כתיבה",
}

# Common words inside values -> Hebrew (whole words, case-insensitive)
WORDS = {
    "integrated": "משולב", "dedicated": "ייעודי", "black": "שחור", "white": "לבן", "silver": "כסוף",
    "grey": "אפור", "gray": "אפור", "blue": "כחול", "red": "אדום", "gold": "זהב",
    "yes": "כן", "no": "לא", "none": "ללא", "optional": "אופציונלי", "up to": "עד",
    "years": "שנים", "year": "שנה", "months": "חודשים", "onsite": "באתר הלקוח", "on-site": "באתר הלקוח",
    "carry-in": "במעבדה", "backlit": "מואר", "with": "עם", "without": "ללא",
    "slots": "חריצים", "slot": "חריץ", "dual-channel capable": "תומך ערוץ כפול", "dual-channel": "ערוץ כפול",
    "anti-glare": "נגד סינוור", "touch": "מגע", "non-touch": "ללא מגע", "free dos": "ללא מערכת הפעלה",
    "freedos": "ללא מערכת הפעלה", "no os": "ללא מערכת הפעלה", "tiny": "מיני", "around": "כ-",
    "approx.": "כ-", "approximately": "כ-",
}
_WORD_RE = re.compile(r"(?<![\w-])(" + "|".join(sorted(map(re.escape, WORDS), key=len, reverse=True)) + r")(?![\w-])",
                      re.I)
_JUNK_LABELS = ("pdf", "datasheet", "download", "קובץ")


def dictionary(rows):
    """Translate what the dictionary knows; everything else is kept as is."""
    out = []
    for r in rows:
        label, value = r["label"].strip(), r["value"].strip()
        if any(j in label.lower() for j in _JUNK_LABELS):
            continue  # "PDF File: download" and the like say nothing to a customer
        label = LABELS.get(label.lower().rstrip(":"), label)
        value = _WORD_RE.sub(lambda m: WORDS[m.group(1).lower()], value)
        out.append({"label": label, "value": value})
    return out
