import pytest

from tradedesk.config import normalize_wa_id, parse_fx
from tradedesk.normalize import (grade_ok, infer_brand, infer_category, loose_key, model_key, normalize_grade,
                                 parse_price, parse_qty, parse_storage, to_base)


@pytest.mark.parametrize("raw,expected", [
    ("Grade A+", "A+"), ("A/A+", "A"), ("like new", "A+"), ("excellent", "A+"), ("A", "A"), ("A-", "A"),
    ("very good", "A"), ("B", "B"), ("b+", "B"), ("good", "B"), ("C", "C"), ("fair", "C"),
    ("ASIS+", "ASIS"), ("grade d", "ASIS"), ("untested", "ASIS"), ("CPO", "CPO"), ("New sealed", "NEW"),
    ("Broken screen", "FAULTY"), ("non-working", "FAULTY"), ("Mixed ABC", "MIXED"), ("", ""), ("xyz", ""),
])
def test_grades(raw, expected):
    assert normalize_grade(raw) == expected


def test_grade_ok():
    assert grade_ok("A", [])
    assert grade_ok("A", ["A+", "A"])
    assert not grade_ok("B", ["A+", "A"])
    assert not grade_ok("", ["A"])


@pytest.mark.parametrize("a,b", [
    (("Apple", "iPhone13 Pro 128GB Grade A+ Sierra Blue"), ("", "iPhone 13 Pro (128 GB)")),
    (("Samsung", "Galaxy S22 SM-S901B 256GB"), ("", "Samsung S22")),
    (("Apple", 'MacBook Pro 13" M1 8GB/256GB'), ("", "MacBook Pro M1 13-inch")),
    (("Dell", "Latitude 5420 i5-1145G7 16GB 256GB SSD"), ("", "dell latitude 5420 i5 1145g7")),
    (("", "iPhone 13 5G"), ("Apple", "iPhone 13")),
])
def test_model_key_same_device(a, b):
    assert model_key(*a) == model_key(*b)


@pytest.mark.parametrize("a,b", [
    ("iPhone 13", "iPhone 13 Pro"),
    ("iPhone 13 Pro", "iPhone 13 Pro Max"),
    ("Galaxy A52", "Galaxy A52 5G"),
    ("Latitude 5420", "Latitude 5430"),
    ("iPhone X", "iPhone XR"),
])
def test_model_key_different_devices(a, b):
    assert model_key("", a) != model_key("", b)


def test_loose_key_ignores_year():
    assert loose_key(model_key("", "MacBook Pro 13 M1 2020")) == model_key("", "MacBook Pro 13 M1")


def test_inference():
    assert (infer_brand("iPhone13 Pro"), infer_category("iPhone13 Pro")) == ("apple", "phone")
    assert infer_category("iPad Air 5") == "tablet"
    assert infer_category("Latitude 5420") == "laptop"
    assert infer_category("OptiPlex 7090 SFF") == "desktop"
    assert infer_category("PowerEdge R740") == "server"
    assert infer_brand("ThinkPad T14") == "lenovo"


@pytest.mark.parametrize("raw,expected", [
    ("€1.234,50", (1234.5, "EUR")), ("GBP 320", (320.0, "GBP")), ("$99.9", (99.9, "USD")), ("320", (320.0, "")),
    ("1,299", (1299.0, "")), ("12,5", (12.5, "")), ("AED 1,200.00", (1200.0, "AED")), (419, (419.0, "")),
    ("", (None, "")), ("TBC", (None, "")),
])
def test_parse_price(raw, expected):
    assert parse_price(raw) == expected


def test_storage_qty_fx():
    assert parse_storage("16GB/512GB") == 512
    assert parse_storage("1TB SSD") == 1024
    assert parse_storage("no storage") is None
    assert parse_qty("50 pcs") == 50 and parse_qty("1,200") == 1200 and parse_qty(None) is None
    assert to_base(100, "GBP", "EUR", {"GBP": 1.17}) == 117.0
    assert to_base(100, "", "EUR", {}) == 100.0
    assert to_base(100, "XYZ", "EUR", {}) is None
    assert parse_fx("GBP=1.17, usd=0.92") == {"GBP": 1.17, "USD": 0.92}


@pytest.mark.parametrize("raw,expected", [
    ("+44 7700 900000", "447700900000"), ("0044 7700 900000", "447700900000"), ("447700900000", "447700900000"),
    ("07700 900000", None), ("", None), ("+49 (0) 151-2345678", "491512345678"),
])
def test_normalize_wa_id(raw, expected):
    assert normalize_wa_id(raw) == expected
