import pytest

from supsearch.normalize import (detect_brand, extract_specs, norm_mpn, parse_price,
                                 parse_stock, with_vat)


@pytest.mark.parametrize("text,expected", [
    ("₪1,234.90", 1234.9),
    ('1,234 ש"ח', 1234.0),
    ("מחיר: 99", 99.0),
    ("‏₪\xa02,499", 2499.0),
    ("", None),
    ("צור קשר", None),
    (0, None),
    (349, 349.0),
])
def test_parse_price(text, expected):
    assert parse_price(text) == expected


def test_with_vat():
    assert with_vat(118.0, True, 0.18) == (118.0, 100.0)
    assert with_vat(100.0, False, 0.18) == (118.0, 100.0)
    assert with_vat(None, True) == (None, None)


@pytest.mark.parametrize("text,expected", [
    ("במלאי", True),
    ("לא במלאי", False),
    ("אזל מהמלאי", False),
    ("זמין למשלוח", True),
    ("3", True),
    ("0", False),
    (True, True),
    ("", None),
    ("צור קשר", None),
    ("https://schema.org/InStock", True),
    ("https://schema.org/OutOfStock", False),
])
def test_parse_stock(text, expected):
    assert parse_stock(text) == expected


def test_norm_mpn():
    assert norm_mpn("82rk-00ab iv") == "82RK00ABIV"
    assert norm_mpn(None) == ""


def test_detect_brand_whole_word():
    assert detect_brand("מחשב נייד Lenovo IdeaPad 3") == "Lenovo"
    assert detect_brand("WD Blue SN580 1TB") == "Western Digital"
    assert detect_brand("Chpad cover") == ""  # 'HP' inside a word is not HP


def test_extract_specs_laptop():
    s = extract_specs('מחשב נייד Lenovo i5-1235U 16GB DDR4 512GB SSD 15.6" Win11 Pro RTX 3050')
    assert s == {"cpu": "Core i5", "ram": "16GB", "storage": "512GB", "screen": '15.6"',
                 "gpu": "RTX 3050", "os": "Windows 11 Pro"}


def test_extract_specs_variants():
    assert extract_specs("Ryzen 7 7735HS 1TB NVMe")["cpu"] == "Ryzen 7"
    assert extract_specs("Ryzen 7 7735HS 1TB NVMe")["storage"] == "1TB"
    assert extract_specs("Core Ultra 7 155H RAM 32GB FreeDOS") == {
        "cpu": "Core Ultra 7", "ram": "32GB", "os": "ללא מערכת הפעלה"}
    assert extract_specs("מסך 27 אינץ' Samsung")["screen"] == '27"'
    assert extract_specs("SSD 2TB Samsung 990 Pro")["storage"] == "2TB"


def test_extract_specs_pipe_separated_titles():
    s = extract_specs("LENOVO M70q G5 i5-14400T | 16GB | 512GB | Dos | 3Y")
    assert s == {"cpu": "Core i5", "ram": "16GB", "storage": "512GB", "os": "ללא מערכת הפעלה"}
    s = extract_specs("LENOVO ThinkCentre neo 30t G5 i5-13420H|16GB|1TB|W11P|3Y")
    assert s["ram"] == "16GB" and s["storage"] == "1TB" and s["os"] == "Windows 11 Pro"
    # a single size is not guessed as RAM or storage
    assert "ram" not in extract_specs("Kingston Fury 16GB kit") and "storage" not in extract_specs("Kingston Fury 16GB kit")


def test_parse_stock_discontinued():
    assert parse_stock("מוצר זה אינו מיוצר יותר") is False
    assert parse_stock("מלאי חסר") is False
    assert parse_stock("זמין במלאי") is True
