from datetime import date

import pytest

from keuangan.categories import match_category
from keuangan.parser import find_amount, parse_expense

TODAY = date(2026, 11, 17)


@pytest.mark.parametrize("text,expected", [
    ("makan siang 35rb", 35_000),
    ("kopi 25k", 25_000),
    ("bensin Rp 50.000", 50_000),
    ("rp50000 parkir", 50_000),
    ("laptop 1,5jt", 1_500_000),
    ("laptop 12 juta", 12_000_000),
    ("makan 35 ribu", 35_000),
    ("belanja bulanan 1.250.000", 1_250_000),
    ("nasi goreng 20000", 20_000),
    ("beli 2 kopi 50rb", 50_000),  # angka kecil tanpa penanda diabaikan
])
def test_amount_formats(text, expected):
    assert parse_expense(text, TODAY).amount == expected


@pytest.mark.parametrize("text", ["halo apa kabar", "beli 2 kopi", "beras 5kg", "x2"])
def test_no_amount(text):
    assert parse_expense(text, TODAY) is None


def test_description_and_category():
    p = parse_expense("makan siang 35rb", TODAY)
    assert (p.description, p.category, p.spent_on) == ("makan siang", "Makanan & Minuman", TODAY)


def test_description_when_only_amount():
    assert parse_expense("35rb", TODAY).description == "Pengeluaran"


def test_unknown_category_is_none():
    assert parse_expense("xyz 10rb", TODAY).category is None


def test_yesterday_and_days_ago():
    assert parse_expense("bensin 50k kemarin", TODAY).spent_on == date(2026, 11, 16)
    p = parse_expense("servis motor 2 hari lalu 300rb", TODAY)
    assert p.spent_on == date(2026, 11, 15) and p.amount == 300_000


def test_find_amount_span_covers_unit():
    value, (s, e) = find_amount("kopi 25rb enak")
    assert value == 25_000 and "kopi 25rb enak"[s:e] == "25rb"


@pytest.mark.parametrize("word,expected", [
    ("makanan", "Makanan & Minuman"), ("Transport", "Transportasi"),
    ("tagihan", "Tagihan & Utilitas"), ("kes", "Kesehatan"), ("ngawur", None),
])
def test_match_category(word, expected):
    assert match_category(word) == expected
