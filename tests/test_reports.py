from datetime import date, timedelta

from keuangan import reports
from tests.conftest import CHAT, TODAY


def week():
    return TODAY - timedelta(days=6), TODAY + timedelta(days=1)


def test_empty_period(db):
    text = reports.summary(db, CHAT, *week(), "Ringkasan 7 hari")
    assert "Belum ada pengeluaran" in text


def test_summary_totals_categories_and_top(db):
    db.add_expense(CHAT, 35_000, "makan siang", "Makanan & Minuman", TODAY)
    db.add_expense(CHAT, 500_000, "sepatu", "Belanja", TODAY - timedelta(days=2))
    text = reports.summary(db, CHAT, *week(), "Ringkasan 7 hari")
    assert "Rp535.000" in text and "2 transaksi" in text
    assert "Belanja: Rp500.000 (93%)" in text
    assert text.index("Belanja") < text.index("Makanan")  # urut terbesar
    assert "• Rp500.000 — sepatu" in text


def test_comparison_with_previous_period(db):
    start, end = week()
    db.add_expense(CHAT, 100_000, "a", "Belanja", start - timedelta(days=1))
    db.add_expense(CHAT, 150_000, "b", "Belanja", TODAY)
    assert "naik 50%" in reports.summary(db, CHAT, start, end, "T")


def test_decrease(db):
    start, end = week()
    db.add_expense(CHAT, 200_000, "a", "Belanja", start - timedelta(days=1))
    db.add_expense(CHAT, 100_000, "b", "Belanja", TODAY)
    assert "turun 50%" in reports.summary(db, CHAT, start, end, "T")


def test_other_chat_excluded(db):
    db.add_expense(2, 999_000, "x", "Belanja", TODAY)
    assert "Belum ada" in reports.summary(db, CHAT, *week(), "T")
