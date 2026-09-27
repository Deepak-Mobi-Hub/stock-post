import io

import pytest
from openpyxl import Workbook

from tradedesk.matcher import Economics, describe, find_opportunities
from tradedesk.stock import read_table_file, rows_to_lines, save_offer, save_requirement

ECON = Economics(base_currency="EUR", fx_rates={"GBP": 1.17, "USD": 0.92}, target_margin_pct=8,
                 cost_per_unit=3, cost_pct=1.5)


def supplier_workbook() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Laptops"
    ws.append(["Echo IT Asset Sales — stock list 24/09"])
    ws.append([])
    ws.append(["Make", "Model", "CPU", "RAM (GB)", "SSD", "Cosmetic", "Keyboard", "Battery", "Qty", "Price EUR"])
    ws.append(["Dell", "Latitude 5420", "i5-1145G7", "16GB", "256GB", "B", "UK", "85%", 40, 210])
    ws.append(["Lenovo", "ThinkPad T14 Gen 2", "i5-1135G7", "8GB", "256GB", "A", "DE", "", 25, "235,00"])
    ws.append(["Total", "", "", "", "", "", "", "", 65, ""])
    ws2 = wb.create_sheet("Phones")
    ws2.append(["Description", "Memory", "Grade", "Quantity", "Price (£)"])
    ws2.append(["iPhone 13 Pro", "128GB", "A+", 50, 419])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_reads_two_tab_supplier_list():
    sheets = read_table_file("stock.xlsx", "", supplier_workbook())
    lines = [ln for name, rows in sheets for ln in rows_to_lines(rows, name)]
    assert len(lines) == 3  # title row and Total row skipped
    dell, lenovo, iphone = lines
    assert dell["model"] == "Latitude 5420" and dell["brand"] == "Dell"
    assert dell["storage_gb"] == 256 and dell["qty"] == 40
    assert dell["unit_price"] == 210 and dell["currency"] == "EUR"
    assert "i5-1145G7" in dell["spec"] and "16GB" in dell["spec"]
    assert "Keyboard: UK" in dell["notes"] and "Battery: 85%" in dell["notes"]
    assert lenovo["unit_price"] == 235.0
    assert iphone["storage_gb"] == 128  # phone lists call storage "Memory"
    assert iphone["currency"] == "GBP"


def test_reads_semicolon_csv_with_decimal_commas():
    data = "Model;Grade;Qty;Price\niPhone 12 64GB;A;30;€229,50\n".encode()
    [(_, rows)] = read_table_file("list.csv", "text/csv", data)
    [line] = rows_to_lines(rows)
    assert line["unit_price"] == 229.5 and line["currency"] == "EUR" and line["storage_gb"] == 64


def test_no_header_means_no_lines():
    assert rows_to_lines([["hello", "world"], ["foo", "bar"]]) == []


@pytest.fixture
def people(db):
    s = db.upsert_contact("491111", name="Markus", company="ITAD GmbH", type="supplier")
    b = db.upsert_contact("331111", name="Elena", company="Refurb SA", type="buyer")
    return s, b


def test_good_margin(db, people):
    s, b = people
    save_offer(db, ECON, s["id"], {"model": "iPhone 13 Pro", "storage_gb": 128, "grade": "A", "qty": 50,
                                   "unit_price": 300, "currency": "EUR"})
    save_requirement(db, ECON, b["id"], {"model": "Apple iPhone 13 Pro 128GB", "grades": ["A+", "A"], "qty": 30,
                                         "target_price": 345, "currency": "EUR"})
    [op] = find_opportunities(db, ECON)
    # costs = 3 + 1.5% of 345 = 8.175 ; profit = 345 - 300 - 8.175 = 36.825 ; margin = 10.67%
    assert op.status == "good"
    assert op.unit_profit == pytest.approx(36.82, abs=0.01)
    assert op.margin_pct == pytest.approx(10.7, abs=0.05)
    assert op.qty == 30 and op.total_profit == pytest.approx(36.82 * 30, abs=0.5)
    assert "Markus (ITAD GmbH)" in describe(op) and "Elena (Refurb SA)" in describe(op)


def test_fx_and_thin_margin(db, people):
    s, b = people
    save_offer(db, ECON, s["id"], {"model": "iPhone 13 Pro", "storage_gb": 128, "grade": "A", "qty": 10,
                                   "unit_price": 280, "currency": "GBP"})  # 327.60 EUR
    save_requirement(db, ECON, b["id"], {"model": "iPhone 13 Pro", "storage_gb": 128, "target_price": 345,
                                         "currency": "EUR"})
    [op] = find_opportunities(db, ECON)
    assert op.buy_price == pytest.approx(327.6)
    assert op.status == "thin"


def test_bid_when_supplier_wants_offer(db, people):
    s, b = people
    save_offer(db, ECON, s["id"], {"model": "Latitude 5420", "grade": "B", "qty": 200})  # "make us an offer"
    save_requirement(db, ECON, b["id"], {"model": "Dell Latitude 5420", "grades": ["B"], "qty": 100,
                                         "target_price": 260, "currency": "EUR"})
    [op] = find_opportunities(db, ECON)
    assert op.status == "needs_buy_price"
    # max bid = 260 * 0.92 - (3 + 3.9) = 232.3
    assert op.max_bid == pytest.approx(232.3, abs=0.01)
    assert "max" in describe(op)


def test_filters_grade_storage_and_same_trader(db, people):
    s, b = people
    save_offer(db, ECON, s["id"], {"model": "iPhone 13", "storage_gb": 128, "grade": "B", "unit_price": 250})
    save_offer(db, ECON, s["id"], {"model": "iPhone 13", "storage_gb": 256, "grade": "A", "unit_price": 300})
    save_offer(db, ECON, b["id"], {"model": "iPhone 13", "storage_gb": 128, "grade": "A", "unit_price": 260})
    save_requirement(db, ECON, b["id"], {"model": "iPhone 13", "storage_gb": 128, "grades": ["A"], "target_price": 330})
    assert find_opportunities(db, ECON) == []  # wrong grade, wrong storage, and the buyer's own stock


def test_near_match_on_year(db, people):
    s, b = people
    save_offer(db, ECON, s["id"], {"model": "MacBook Pro 13 M1 2020", "grade": "A", "unit_price": 500})
    save_requirement(db, ECON, b["id"], {"model": "MacBook Pro M1 13", "target_price": 600})
    [op] = find_opportunities(db, ECON)
    assert op.near_match and "check spec" in describe(op)


def test_sell_price_hits_target_margin():
    sell = ECON.sell_price(300)
    margin = (sell - 300 - ECON.costs(sell)) / sell * 100
    assert margin == pytest.approx(ECON.target_margin_pct, abs=0.01)
