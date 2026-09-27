"""Normalise messy trade data: grades, storage, model names, prices, currencies.

Suppliers and buyers write the same device a dozen ways ("iPhone13 128gb A+",
"Apple iPhone 13 (128 GB) Grade A+ Midnight"). Matching supply to demand only
works once both collapse to the same model key, storage and grade.
"""

from __future__ import annotations

import re

# Canonical grades, best to worst. Rank is used for "grade X or better" matching.
GRADE_RANK = {"NEW": 7, "CPO": 6, "A+": 5, "A": 4, "B": 3, "MIXED": 2, "C": 2, "ASIS": 1, "FAULTY": 0}

_FAULTY = re.compile(r"faulty|broken|defect|non[- ]?work|not work|damaged|cracked|for parts|spares|bad (lcd|screen)|dead")
_NEW = re.compile(r"\b(brand new|new|sealed|bnib|nib)\b")
_CPO = re.compile(r"\bcpo\b|certified")
_MIXED = re.compile(r"\bmixed\b|\babc\b|a/b/c|a-c\b")
_ASIS = re.compile(r"as[- ]?is|\bd\b|untested")
_LIKE_NEW = re.compile(r"like new|as new")
_APLUS = re.compile(r"a\+|premium|pristine|excellent|mint")
_A = re.compile(r"\ba(?![+\w])|very good")
_B = re.compile(r"\bb[+-]?(?=\W|$)|(?<!very )\bgood\b")
_C = re.compile(r"\bc[+-]?(?=\W|$)|\bfair\b|acceptable")


def normalize_grade(raw: str | None) -> str:
    """Map a free-text grade to NEW/CPO/A+/A/B/C/MIXED/ASIS/FAULTY ('' if unknown).

    Combined grades like "A/A+" resolve to the lower one — safest for pricing.
    """
    if not raw:
        return ""
    s = str(raw).lower().replace("grade", " ").strip()
    if not s:
        return ""
    if _FAULTY.search(s):
        return "FAULTY"
    if _MIXED.search(s):
        return "MIXED"
    if _ASIS.search(s):
        return "ASIS"
    if _CPO.search(s):
        return "CPO"
    if _LIKE_NEW.search(s):
        return "A+"
    if _NEW.search(s):
        return "NEW"
    found = [g for g, rx in (("C", _C), ("B", _B), ("A+", _APLUS), ("A", _A)) if rx.search(s)]
    if not found:
        return ""
    # A/A+ -> A; B/C -> C; pick the worst mentioned
    return min(found, key=lambda g: GRADE_RANK[g])


def grade_ok(offer_grade: str, accepted: list[str]) -> bool:
    """True if an offer's grade is acceptable to a buyer. Empty accepted list = any grade."""
    if not accepted:
        return True
    if not offer_grade:
        return False
    return offer_grade in accepted


_STORAGE = re.compile(r"(\d+(?:\.\d+)?)\s*(tb|gb)\b", re.I)


def parse_storage(raw: str | None) -> int | None:
    """Largest capacity mentioned, in GB ("16GB/512GB" -> 512, "1TB" -> 1024)."""
    if not raw:
        return None
    sizes = []
    for num, unit in _STORAGE.findall(str(raw)):
        gb = float(num) * (1024 if unit.lower() == "tb" else 1)
        sizes.append(int(gb))
    return max(sizes) if sizes else None


BRAND_HINTS = [
    ("apple", r"\b(apple|iphone|ipad|macbook|imac|mac mini|mac studio|airpods|apple watch)\b"),
    ("samsung", r"\b(samsung|galaxy)\b"),
    ("dell", r"\b(dell|latitude|precision|optiplex|xps|inspiron|vostro|poweredge)\b"),
    ("hp", r"\b(hp|hewlett|elitebook|probook|zbook|elitedesk|prodesk|elite dragonfly|proliant)\b"),
    ("lenovo", r"\b(lenovo|thinkpad|thinkcentre|thinkbook|ideapad|thinksystem)\b"),
    ("microsoft", r"\b(microsoft|surface)\b"),
    ("google", r"\b(google|pixel)\b"),
    ("fujitsu", r"\b(fujitsu|lifebook|esprimo)\b"),
    ("xiaomi", r"\b(xiaomi|redmi|poco)\b"),
    ("oneplus", r"\boneplus\b"),
    ("huawei", r"\bhuawei\b"),
    ("sony", r"\b(sony|xperia)\b"),
    ("acer", r"\bacer\b"),
    ("asus", r"\basus\b"),
    ("panasonic", r"\b(panasonic|toughbook)\b"),
]

CATEGORY_HINTS = [
    ("server", r"\b(server|poweredge|proliant|thinksystem)\b"),
    ("tablet", r"\b(ipad|galaxy tab|tab s\d|tablet)\b"),
    ("wearable", r"\b(watch|airpods)\b"),
    ("desktop", r"\b(imac|mac mini|mac studio|optiplex|elitedesk|prodesk|thinkcentre|esprimo|desktop|sff|tower|aio)\b"),
    ("laptop", r"\b(macbook|latitude|precision|xps|elitebook|probook|zbook|thinkpad|thinkbook|surface (pro|laptop|book)|"
               r"lifebook|chromebook|notebook|laptop|inspiron|vostro|ideapad|toughbook|dragonfly)\b"),
    ("phone", r"\b(iphone|galaxy [asznm]\d*|galaxy note|galaxy z|pixel|xperia|redmi|oneplus|phone|mobile|s\d{2}|z ?(fold|flip)\d?)\b"),
    ("accessory", r"\b(charger|adapter|dock|keyboard|mouse|cable|case)\b"),
]


def _spaced(text: str) -> str:
    return re.sub(r"(iphone|galaxy|pixel|ipad)(\d)", r"\1 \2", text.lower())


def infer_brand(text: str) -> str:
    s = _spaced(text)
    for brand, pattern in BRAND_HINTS:
        if re.search(pattern, s):
            return brand
    return ""


def infer_category(text: str) -> str:
    s = _spaced(text)
    for category, pattern in CATEGORY_HINTS:
        if re.search(pattern, s):
            return category
    return ""


_COLOURS = (
    "space gray|space grey|sierra blue|alpine green|deep purple|rose gold|product red|natural titanium|"
    "blue titanium|white titanium|black titanium|desert titanium|midnight green|pacific blue|"
    "black|white|silver|gold|blue|red|green|purple|pink|graphite|midnight|starlight|yellow|coral|"
    "titanium|grey|gray|lavender|cream|phantom|violet"
)
_NOISE = re.compile(
    rf"\b({_COLOURS})\b"
    r"|\b\d+(?:\.\d+)?\s*(gb|tb)\b"
    r"|\b(grade|cpo|asis|used|refurbished|refurb|unlocked|locked|sealed|dual sim|esim|pcs|units|qty|"
    r"ram|ssd|hdd|storage|wifi|wi-fi|cellular|lte|with|without|box|original)\b"
    r"|\b[a-d][+-]?(?=\s|$)",
    re.I,
)


_PART_CODES = re.compile(r"\bsm-?[a-z]\d{3,}\w*|\ba\d{4}\b|\b[a-z]{2,4}\d{2,3}[a-z]{1,2}/a\b")
_DROP_TOKENS = {"inch", "in", "galaxy", "apple", "series", "gen", "generation"}
_YEAR = re.compile(r"^(20[0-3]\d)$")


def model_key(brand: str | None, model: str | None) -> str:
    """Canonical key used to match the same device across suppliers and buyers.

    Keeps what changes the price (model, generation, screen size, 5G on Android,
    chip) and drops what doesn't identify the model (colour, grade, storage —
    storage is matched separately — and part codes). Tokens are sorted so word
    order doesn't matter: "MacBook Pro 13 M1" == "MacBook Pro M1 13-inch".
    """
    raw = f"{brand or ''} {model or ''}".lower()
    brand_name = (brand or "").strip().lower() or infer_brand(raw)
    text = _PART_CODES.sub(" ", raw)
    text = re.sub(r"(iphone|galaxy|pixel|ipad)(\d)", r"\1 \2", text)
    text = re.sub(r"(\d)(inch|in|\")", r"\1 ", text)
    text = re.sub(r"[()\[\],/|_\"']", " ", text)
    text = _NOISE.sub(" ", text)
    text = re.sub(r"[^a-z0-9+ ]", " ", text)
    tokens = [t for t in text.split() if t and t not in _DROP_TOKENS and t != brand_name]
    if brand_name == "apple":
        tokens = [t for t in tokens if t != "5g"]  # every recent iPhone is 5G; it's noise there
    key = " ".join(sorted(set(tokens)))
    return f"{brand_name} {key}".strip() if brand_name else key


def loose_key(key: str) -> str:
    """Model key without year tokens, for near-matches ("... 2020" vs no year)."""
    return " ".join(t for t in key.split() if not _YEAR.match(t))


CURRENCY_SYMBOLS = {"€": "EUR", "£": "GBP", "$": "USD", "د.إ": "AED"}
CURRENCY_CODES = {"EUR", "GBP", "USD", "AED", "CHF", "SEK", "NOK", "DKK", "PLN", "CZK", "HUF", "NZD", "AUD", "CAD"}


def parse_price(raw) -> tuple[float | None, str]:
    """Parse "€1.234,50", "GBP 320", "$99.9" -> (amount, currency code or '')."""
    if raw is None or raw == "":
        return None, ""
    if isinstance(raw, (int, float)):
        return float(raw), ""
    s = str(raw).strip()
    currency = ""
    for sym, code in CURRENCY_SYMBOLS.items():
        if sym in s:
            currency = code
            break
    if not currency:
        m = re.search(r"\b([A-Za-z]{3})\b", s)
        if m and m.group(1).upper() in CURRENCY_CODES:
            currency = m.group(1).upper()
    num = re.sub(r"[^0-9.,]", "", s)
    if not num or not re.search(r"\d", num):
        return None, currency
    if "," in num and "." in num:
        # whichever separator comes last is the decimal one
        if num.rfind(",") > num.rfind("."):
            num = num.replace(".", "").replace(",", ".")
        else:
            num = num.replace(",", "")
    elif "," in num:
        head, _, tail = num.rpartition(",")
        num = f"{head.replace(',', '')}.{tail}" if len(tail) in (1, 2) else num.replace(",", "")
    try:
        return float(num), currency
    except ValueError:
        return None, currency


def to_base(amount: float | None, currency: str | None, base: str, fx: dict[str, float]) -> float | None:
    """Convert to the base currency. None if the amount or rate is unknown."""
    if amount is None:
        return None
    code = (currency or "").upper() or base
    if code == base:
        return round(float(amount), 2)
    rate = fx.get(code)
    return round(float(amount) * rate, 2) if rate else None


def parse_qty(raw) -> int | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    m = re.search(r"\d[\d,.]*", str(raw))
    if not m:
        return None
    try:
        return int(float(m.group(0).replace(",", "")))
    except ValueError:
        return None
