"""Supply × demand: which supplier stock can we sell to which buyer, at what margin.

For every open buyer requirement and every available supplier line on the same
model (same storage when both are known, grade acceptable to the buyer), work
out unit profit after costs, margin %, total profit, and — when the supplier
hasn't priced the lot — the most we can bid and still hit target margin.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from .config import Config
from .db import Database
from .normalize import grade_ok, loose_key


@dataclass
class Economics:
    base_currency: str
    fx_rates: dict[str, float]
    target_margin_pct: float
    cost_per_unit: float
    cost_pct: float

    def costs(self, sell: float) -> float:
        return self.cost_per_unit + sell * self.cost_pct / 100

    def max_bid(self, sell: float) -> float:
        """Highest buy price that still leaves target margin on this sell price."""
        return round(sell * (1 - self.target_margin_pct / 100) - self.costs(sell), 2)

    def sell_price(self, buy: float) -> float:
        """Sell price that gives exactly target margin on a given buy price."""
        denom = 1 - (self.target_margin_pct + self.cost_pct) / 100
        return round((buy + self.cost_per_unit) / denom, 2) if denom > 0 else buy


SETTING_KEYS = ("target_margin_pct", "cost_per_unit", "cost_pct", "fx_rates", "base_currency", "mode")


def economics(db: Database, config: Config) -> Economics:
    """Env defaults, overridden by anything saved from the dashboard."""
    def num(key: str, default: float) -> float:
        raw = db.get_setting(key)
        return float(raw) if raw not in (None, "") else default

    fx_raw = db.get_setting("fx_rates")
    fx = json.loads(fx_raw) if fx_raw else dict(config.fx_rates)
    return Economics(
        base_currency=db.get_setting("base_currency") or config.base_currency,
        fx_rates=fx,
        target_margin_pct=num("target_margin_pct", config.target_margin_pct),
        cost_per_unit=num("cost_per_unit", config.cost_per_unit),
        cost_pct=num("cost_pct", config.cost_pct),
    )


@dataclass
class Opportunity:
    offer_id: int
    requirement_id: int
    model: str
    storage_gb: int | None
    grade: str
    offer_spec: str
    offer_notes: str
    requirement_spec: str
    supplier_id: int | None
    supplier: str
    buyer_id: int | None
    buyer: str
    available_qty: int | None
    wanted_qty: int | None
    qty: int | None
    buy_price: float | None
    sell_price: float | None
    unit_profit: float | None
    margin_pct: float | None
    total_profit: float | None
    max_bid: float | None
    status: str          # good | thin | negative | needs_buy_price | needs_sell_price | needs_both
    near_match: bool     # matched ignoring year — check the spec before trading
    currency: str

    def to_dict(self) -> dict:
        return asdict(self)


def _label(row: dict, prefix: str) -> str:
    name = row.get(f"{prefix}_name") or ""
    company = row.get(f"{prefix}_company") or ""
    if name and company:
        return f"{name} ({company})"
    return name or company or row.get(f"{prefix}_wa") or "—"


def _accepted_grades(req: dict) -> list[str]:
    return [g.strip() for g in (req.get("grades") or "").split(",") if g.strip()]


def find_opportunities(db: Database, econ: Economics, *, offer_ids: list[int] | None = None,
                       requirement_ids: list[int] | None = None) -> list[Opportunity]:
    offers = db.all(
        "SELECT o.*, c.name AS s_name, c.company AS s_company, c.wa_id AS s_wa FROM offers o "
        "LEFT JOIN contacts c ON c.id = o.contact_id WHERE o.status = 'available' AND o.model_key != ''")
    reqs = db.all(
        "SELECT r.*, c.name AS b_name, c.company AS b_company, c.wa_id AS b_wa FROM requirements r "
        "LEFT JOIN contacts c ON c.id = r.contact_id WHERE r.status = 'open' AND r.model_key != ''")
    if offer_ids is not None:
        offers = [o for o in offers if o["id"] in offer_ids]
    if requirement_ids is not None:
        reqs = [r for r in reqs if r["id"] in requirement_ids]

    by_key: dict[str, list[dict]] = {}
    by_loose: dict[str, list[dict]] = {}
    for o in offers:
        by_key.setdefault(o["model_key"], []).append(o)
        by_loose.setdefault(loose_key(o["model_key"]), []).append(o)

    results: list[Opportunity] = []
    for r in reqs:
        exact = by_key.get(r["model_key"], [])
        near = [o for o in by_loose.get(loose_key(r["model_key"]), []) if o not in exact]
        for o, is_near in [(o, False) for o in exact] + [(o, True) for o in near]:
            if o["contact_id"] and o["contact_id"] == r["contact_id"]:
                continue  # same trader on both sides
            if o["storage_gb"] and r["storage_gb"] and o["storage_gb"] != r["storage_gb"]:
                continue
            if not grade_ok(o["grade_norm"], _accepted_grades(r)):
                continue
            results.append(_evaluate(o, r, econ, is_near))

    order = {"good": 0, "needs_buy_price": 1, "thin": 2, "needs_sell_price": 3, "needs_both": 4, "negative": 5}
    results.sort(key=lambda op: (order.get(op.status, 9), -(op.total_profit or 0), -(op.margin_pct or 0)))
    return results


def _evaluate(o: dict, r: dict, econ: Economics, near: bool) -> Opportunity:
    buy, sell = o["price_base"], r["price_base"]
    avail, wanted = o["qty"], r["qty"]
    qty = min(avail, wanted) if avail and wanted else (avail or wanted)
    unit_profit = margin = total = None
    max_bid = econ.max_bid(sell) if sell is not None else None
    if buy is not None and sell is not None and sell > 0:
        unit_profit = round(sell - buy - econ.costs(sell), 2)
        margin = round(unit_profit / sell * 100, 1)
        total = round(unit_profit * qty, 2) if qty else None
        status = "good" if margin >= econ.target_margin_pct else ("thin" if unit_profit > 0 else "negative")
    elif sell is not None:
        status = "needs_buy_price"   # supplier wants an offer: max_bid tells Deepak what he can pay
    elif buy is not None:
        status = "needs_sell_price"  # ask the buyer for a target price
    else:
        status = "needs_both"
    return Opportunity(
        offer_id=o["id"], requirement_id=r["id"],
        model=o["model"] or r["model"], storage_gb=o["storage_gb"] or r["storage_gb"],
        grade=o["grade_norm"] or o["grade"], offer_spec=o["spec"] or "", offer_notes=o["notes"] or "",
        requirement_spec=" · ".join(x for x in [r["spec"], r["notes"]] if x),
        supplier_id=o["contact_id"], supplier=_label(o, "s"),
        buyer_id=r["contact_id"], buyer=_label(r, "b"),
        available_qty=avail, wanted_qty=wanted, qty=qty,
        buy_price=buy, sell_price=sell, unit_profit=unit_profit, margin_pct=margin,
        total_profit=total, max_bid=max_bid, status=status, near_match=near,
        currency=econ.base_currency,
    )


def money(amount: float | None, currency: str) -> str:
    if amount is None:
        return "?"
    symbol = {"EUR": "€", "GBP": "£", "USD": "$"}.get(currency, currency + " ")
    return f"{symbol}{amount:,.0f}" if abs(amount) >= 100 else f"{symbol}{amount:,.2f}"


def describe(op: Opportunity) -> str:
    """One line for WhatsApp / digest."""
    spec = f" {op.storage_gb}GB" if op.storage_gb else ""
    qty = f" ×{op.qty}" if op.qty else ""
    head = f"{op.model}{spec} {op.grade}{qty}".strip()
    if op.status in ("good", "thin", "negative"):
        line = (f"{head}: buy {money(op.buy_price, op.currency)} ({op.supplier}) → sell "
                f"{money(op.sell_price, op.currency)} ({op.buyer}) · {op.margin_pct}%")
        if op.total_profit is not None:
            line += f" · ~{money(op.total_profit, op.currency)}"
    elif op.status == "needs_buy_price":
        line = (f"{head}: {op.buyer} pays {money(op.sell_price, op.currency)} → bid {op.supplier} "
                f"max {money(op.max_bid, op.currency)}")
    elif op.status == "needs_sell_price":
        line = f"{head}: {op.supplier} at {money(op.buy_price, op.currency)} — get a target price from {op.buyer}"
    else:
        line = f"{head}: {op.supplier} ↔ {op.buyer} — no prices yet"
    if op.near_match:
        line += " (check spec/year)"
    return line
