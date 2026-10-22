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


@pytest.mark.parametrize("title,expected", [
    # real distributor titles
    ("LENOVO ThinkPad L16 G3 U7-355|64GB 1TB|W11P|3YRW",
     {"cpu": "Core Ultra 7", "ram": "64GB", "storage": "1TB", "os": "Windows 11 Pro"}),
    ("LENOVO ThinkPad X1 G14 UX7-358H|Touch|64GB|2TB|W11P|3YW", {"cpu": "Core Ultra X7"}),
    ("נייח LENOVO ThinkCenter neo 50q Gen6 CORE U7-256V 16GB 512GB DOS", {"cpu": "Core Ultra 7"}),
    ("LENOVO V15 G4 Ryzen-7 5825U|8GB|512GB|DOS|1YR", {"cpu": "Ryzen 7"}),
    ("נייד Lenovo V15 R5 5625U 8GB 512GB Radeon 15.6 TN 60Hz DOS", {"cpu": "Ryzen 5"}),
    ("LENOVO ThinkPad P14s G7 AMD 9 HX PRO 470|Touch|48GB|1TB|W11P|3YRW", {"cpu": "Ryzen 9", "ram": "48GB"}),
    ("נייד Lenovo V14 Core 5 210H 8GB 256GB Intel 14 FHD 60Hz DOS Black", {"cpu": "Core 5", "ram": "8GB"}),
    ("נייח LENOVO ThinkCentre neo 50q Gen 5 CORE 7-240H 16GB 512NVME DO",
     {"cpu": "Core 7", "ram": "16GB", "storage": "512GB"}),
    ("‏נייח Lenovo ThinkCentre neo 50q G6 U5-226V 16GB 512GB Win11P 3Y‏", {"os": "Windows 11 Pro"}),
    ("מסך ThinkVision S24-4e 4ms 100Hz FHD IPS 23.8 HDMI VGA Black Vesa", {"screen": '23.8"'}),
    ("מסך LENOVO ThinkVision S27-4e FHD IPS 100Hz 4ms Vesa HDMI VGA", {"screen": '27"'}),
    ("Lenovo monitor L27h-4A - 67C0UAC6IS", {"screen": '27"'}),
    ("LENOVO P16v G3 U7 255H | 32GB | 1TB | RTX PRO 2000 | W11P", {"cpu": "Core Ultra 7", "gpu": "RTX PRO 2000"}),
    ("LENOVO ThinkPad P16 Gen 3 U7-255HX|32GB|1TB|RTX1000|W11P|3Y", {"gpu": "RTX 1000"}),
])
def test_extract_specs_real_titles(title, expected):
    specs = extract_specs(title)
    assert {k: specs.get(k) for k in expected} == expected


def test_no_false_specs_on_accessories():
    assert "screen" not in extract_specs("תומך לכרטיס מסך Antec GPU Holder IGPU Black")
    assert "cpu" not in extract_specs("מטען למחשב נייד LENOVO 65W TYPE-C")
    assert extract_specs("Kingston Fury 16GB kit") == {}


def test_pipe_fields_and_monitor_model_codes():
    s = extract_specs("HP ProBook 4 G2i 16 U7-355|24G|512G|16|DOS|3Y")
    assert (s["ram"], s["storage"], s["screen"]) == ("24GB", "512GB", '16"')
    assert extract_specs("מסך Lenovo C27-40 FHD 4ms 75hz VA HDMI VGA")["screen"] == '27"'
    assert extract_specs("מסך SAMSUNG S24HG302EU 24 IPS FHD 200Hz")["screen"] == '24"'
    # "4G LTE" in free text is not a memory size
    assert extract_specs("ThinkPad X13 4G LTE 16GB 512GB")["ram"] == "16GB"


def test_apple_silicon_needs_apple_context():
    assert "cpu" not in extract_specs("מסך מחשב Dell SE2425H FHD 75Hz 5 Ms VA VGA HDMI 300cd/m2")
    assert "cpu" not in extract_specs("Samsung SSD M2 NVMe 1TB")
    assert extract_specs("Apple MacBook Air 13 M3 16GB 512GB")["cpu"] == "Apple M3"
    assert extract_specs("MacBook Pro 14 M4 Pro 24GB 1TB")["cpu"] == "Apple M4 Pro"


def test_canonical_brand():
    from supsearch.normalize import canonical_brand
    assert canonical_brand("LENOVO") == "Lenovo"
    assert canonical_brand("HP") == "HP"
    assert canonical_brand("GENERIC") == "Generic"
    assert canonical_brand("MAIWO") == "Maiwo"
    assert canonical_brand("") == ""


@pytest.mark.parametrize("title,expected", [
    # Usys (Unitech Systems) titles: bare G/T sizes, ″ for inches, C5- for Core 5, FD for FreeDOS
    ("Lenovo V14 G4 14″ i5-13420H 16G D5 512G FD Black 1Y",
     {"cpu": "Core i5", "ram": "16GB", "storage": "512GB", "screen": '14"', "os": "ללא מערכת הפעלה"}),
    ("ASUS E1504FA 15.6 R5-7520U 8G 2T FD Black 1Y", {"cpu": "Ryzen 5", "ram": "8GB", "storage": "2TB"}),
    ("ASUS P500MV C5-210H 32G D5 1T HDMI DP FD 3YOS", {"cpu": "Core 5", "ram": "32GB", "storage": "1TB"}),
    ("H610M i5-14400F 16G SSD 500G PCIe NVMe Case", {"cpu": "Core i5", "ram": "16GB", "storage": "500GB"}),
])
def test_extract_specs_bare_units(title, expected):
    specs = extract_specs(title)
    assert {k: specs.get(k) for k in expected} == expected


def test_bare_units_need_a_computer_title():
    assert "ram" not in extract_specs("Huawei 4G router B311 8G")      # no CPU: not a computer
    assert extract_specs("ThinkPad i5 4G LTE 16G 512G")["ram"] == "16GB"  # 4G LTE is the modem
    from supsearch.normalize import canonical_brand
    assert canonical_brand("barkan") == "Barkan"


@pytest.mark.parametrize("text,expected", [
    ("$1,234.00", "USD"), ("1,234 USD", "USD"), ("₪1,234", "ILS"), ('1,234 ש"ח', "ILS"),
    ("€99", "EUR"), ("1,234.00", None), ("", None),
])
def test_detect_currency(text, expected):
    from supsearch.normalize import detect_currency
    assert detect_currency(text) == expected
