"""Stock lists in, clean offer/requirement rows out.

Suppliers send Excel files with headers on row 3, two tabs, prices in a column
called "EUR", grades called "Cosmetic". This module finds the header row, maps
columns by synonym and turns rows into offer lines. When a sheet makes no
sense to the column mapper, the caller falls back to Claude extraction.
"""

from __future__ import annotations

import csv
import io
import re
from typing import Iterable

from .db import Database, iso
from .matcher import Economics
from .normalize import (infer_brand, infer_category, model_key, normalize_grade, parse_price,
                        parse_qty, parse_storage, to_base)

HEADER_SYNONYMS: dict[str, tuple[str, ...]] = {
    # order matters: first field whose synonym matches wins ("RAM (GB)" is RAM, not storage)
    "model": ("model", "description", "product", "item", "device", "product name", "model name", "desc",
              "article", "title", "item description", "product description"),
    "brand": ("brand", "make", "manufacturer", "oem"),
    "ram": ("ram", "memory"),
    "cpu": ("cpu", "processor", "proc", "chip"),
    "storage": ("storage", "capacity", "rom", "hdd", "ssd", "disk"),
    "grade": ("grade", "condition", "cosmetic", "cosmetic grade", "quality", "cond"),
    "qty": ("qty", "quantity", "units", "pcs", "stock", "available", "count", "qnty", "avail", "amount"),
    "price": ("price", "unit price", "cost", "offer", "ask", "asking", "eur", "gbp", "usd", "€", "£", "$",
              "price/unit", "unit cost", "sell price", "target price", "target"),
    "colour": ("colour", "color"),
    "location": ("location", "warehouse", "origin", "stock location"),
    "notes": ("notes", "comments", "remarks", "comment", "info", "functionality", "fault", "faults", "defects",
              "keyboard", "layout", "battery", "lock", "locks", "status"),
}


def _norm_header(cell) -> str:
    return re.sub(r"\s+", " ", str(cell or "").strip().lower())


def _match_header(cell) -> str | None:
    h = _norm_header(cell)
    if not h:
        return None
    for field, words in HEADER_SYNONYMS.items():
        for w in words:
            if h == w or re.search(rf"(^|[\s(/_-]){re.escape(w)}($|[\s)/_:-])", h):
                return field
    return None


def _header_currency(cell) -> str:
    _, cur = parse_price(str(cell or "") + " 1")
    return cur


def rows_to_lines(rows: list[list], sheet: str = "") -> list[dict]:
    """Find the header row and map data rows to offer-line dicts. [] if no usable header."""
    best_idx, best_map, best_score = -1, {}, 0
    for idx, row in enumerate(rows[:25]):
        mapping: dict[int, str] = {}
        for col, cell in enumerate(row):
            field = _match_header(cell)
            if field and (field == "notes" or field not in mapping.values()):
                mapping[col] = field
        score = len(mapping)
        if score > best_score and "model" in mapping.values():
            best_idx, best_map, best_score = idx, mapping, score
    if best_score < 2:
        return []

    header = rows[best_idx]
    price_col = next((c for c, f in best_map.items() if f == "price"), None)
    default_currency = _header_currency(header[price_col]) if price_col is not None else ""

    lines = []
    for row in rows[best_idx + 1:]:
        cells = {f: row[c] if c < len(row) else None for c, f in best_map.items() if f != "notes"}
        extras = [f"{str(header[c]).strip()}: {str(row[c]).strip()}" for c, f in best_map.items()
                  if f == "notes" and c < len(row) and row[c] not in (None, "")]
        model = str(cells.get("model") or "").strip()
        if not model or re.match(r"(?i)^(total|subtotal|sum)\b", model):
            continue
        spec_parts = [str(cells[k]).strip() for k in ("cpu", "ram", "storage", "colour")
                      if cells.get(k) not in (None, "")]
        price, currency = parse_price(cells.get("price"))
        brand = str(cells.get("brand") or "").strip()
        storage = parse_storage(f"{cells.get('storage') or ''}") or parse_storage(model)
        if storage is None and cells.get("ram") not in (None, ""):
            # phone lists often call storage "Memory": 128GB of RAM isn't a thing
            mem = parse_storage(f"{cells['ram']}") or parse_qty(cells["ram"])
            small_device = infer_category(f"{brand} {model}") in ("phone", "tablet")
            if mem and (mem >= 64 or (small_device and mem >= 16)):
                storage = mem
        lines.append({
            "brand": brand,
            "model": model,
            "storage_gb": storage,
            "spec": " / ".join(spec_parts),
            "grade": str(cells.get("grade") or "").strip(),
            "qty": parse_qty(cells.get("qty")),
            "unit_price": price,
            "currency": currency or default_currency,
            "location": str(cells.get("location") or "").strip(),
            "notes": "; ".join(extras + ([f"sheet: {sheet}"] if sheet else [])),
        })
    return lines


def read_xlsx(data: bytes) -> list[tuple[str, list[list]]]:
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    sheets = []
    for ws in wb.worksheets:
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        rows = [r for r in rows if any(c not in (None, "") for c in r)]
        if rows:
            sheets.append((ws.title, rows))
    return sheets


def read_csv(data: bytes) -> list[tuple[str, list[list]]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
    return [("csv", rows)] if rows else []


def read_table_file(filename: str, mime: str, data: bytes) -> list[tuple[str, list[list]]] | None:
    """Return sheets for spreadsheet/CSV files, None for anything else."""
    name = (filename or "").lower()
    if name.endswith(".xlsx") or "spreadsheetml" in (mime or ""):
        return read_xlsx(data)
    if name.endswith((".csv", ".tsv", ".txt")) or (mime or "") in ("text/csv", "text/plain", "text/tab-separated-values"):
        return read_csv(data)
    return None


def sheets_to_text(sheets: list[tuple[str, list[list]]], limit_chars: int = 60_000) -> tuple[str, bool]:
    """Tab-separated text of the sheets for Claude. Returns (text, truncated)."""
    out = []
    for name, rows in sheets:
        out.append(f"## Sheet: {name}")
        for r in rows:
            out.append("\t".join("" if c is None else str(c) for c in r))
    text = "\n".join(out)
    return (text[:limit_chars], True) if len(text) > limit_chars else (text, False)


COUNTRY_CURRENCY = {
    "uk": "GBP", "united kingdom": "GBP", "gb": "GBP", "england": "GBP", "scotland": "GBP", "wales": "GBP",
    "us": "USD", "usa": "USD", "united states": "USD",
    "uae": "AED", "united arab emirates": "AED", "dubai": "AED",
}


def currency_for(contact: dict | None, base: str) -> str:
    country = ((contact or {}).get("country") or "").strip().lower()
    return COUNTRY_CURRENCY.get(country, base)


# ------------------------------------------------------------------- saving
def _common(line: dict, econ: Economics, default_currency: str) -> dict:
    model = str(line.get("model") or "").strip()
    brand = str(line.get("brand") or "").strip() or infer_brand(model)
    spec = str(line.get("spec") or "").strip()
    currency = (line.get("currency") or "").upper() or default_currency
    return {
        "category": line.get("category") or infer_category(f"{brand} {model} {spec}"),
        "brand": brand,
        "model": model,
        "model_key": model_key(brand, model),
        "storage_gb": line.get("storage_gb") or parse_storage(f"{model} {spec}"),
        "spec": spec,
        "currency": currency,
        "notes": str(line.get("notes") or "").strip(),
        "created_at": iso(),
    }


def save_offer(db: Database, econ: Economics, contact_id: int | None, line: dict, *,
               source: str = "", default_currency: str | None = None) -> int | None:
    base = _common(line, econ, default_currency or econ.base_currency)
    if not base["model"]:
        return None
    price = line.get("unit_price")
    grade = str(line.get("grade") or "").strip()
    base.update({
        "contact_id": contact_id,
        "grade": grade,
        "grade_norm": normalize_grade(grade),
        "qty": line.get("qty"),
        "unit_price": price,
        "price_base": to_base(price, base["currency"], econ.base_currency, econ.fx_rates),
        "location": str(line.get("location") or "").strip(),
        "source": source,
    })
    return db.insert("offers", base)


def save_requirement(db: Database, econ: Economics, contact_id: int | None, line: dict, *,
                     source: str = "", default_currency: str | None = None) -> int | None:
    base = _common(line, econ, default_currency or econ.base_currency)
    if not base["model"]:
        return None
    grades = line.get("grades") or []
    if isinstance(grades, str):
        grades = re.split(r"[,/]", grades)
    norm = sorted({normalize_grade(g) for g in grades if normalize_grade(g)})
    price = line.get("target_price")
    base.update({
        "contact_id": contact_id,
        "grades": ",".join(norm),
        "qty": line.get("qty"),
        "target_price": price,
        "price_base": to_base(price, base["currency"], econ.base_currency, econ.fx_rates),
        "destination": str(line.get("destination") or "").strip(),
    })
    return db.insert("requirements", base)


def import_offer_lines(db: Database, econ: Economics, contact_id: int | None, lines: Iterable[dict], *,
                       source: str, replace_previous_lists: bool = True,
                       default_currency: str | None = None) -> list[int]:
    """Save a full stock list. A supplier's new list supersedes their previous file-based lists."""
    lines = list(lines)
    if replace_previous_lists and contact_id and lines:
        db.execute("UPDATE offers SET status = 'expired' WHERE contact_id = ? AND status = 'available' "
                   "AND source LIKE 'file:%'", (contact_id,))
    ids = []
    for line in lines:
        oid = save_offer(db, econ, contact_id, line, source=source, default_currency=default_currency)
        if oid:
            ids.append(oid)
    return ids
