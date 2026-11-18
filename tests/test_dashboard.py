from datetime import date, timedelta

import pytest

from keuangan import dashboard
from keuangan.dashboard import build_summary, list_transactions
from tests.conftest import CHAT, TODAY


def d(n):
    return TODAY - timedelta(days=n)


@pytest.fixture
def seeded(db):
    db.add_expense(CHAT, 25_000, "kopi kenangan", "Makanan & Minuman", d(0))
    db.add_expense(CHAT, 500_000, "sepatu", "Belanja", d(5))
    db.add_expense(CHAT, 120_000, "makan malam", "Makanan & Minuman", d(2))
    db.add_expense(CHAT, 90_000, "gojek", "Transportasi", d(20))      # Okt
    db.add_expense(CHAT, 40_000, "kopi lagi", "Makanan & Minuman", d(40))  # Okt
    db.add_expense(2, 999_000, "milik orang lain", "Belanja", d(0))
    db.add_income(CHAT, 8_000_000, "gaji", d(10))
    db.add_income(CHAT, 1_500_000, "freelance logo", d(3))
    db.add_income(2, 7_000_000, "gaji orang lain", d(1))
    db.set_budget(CHAT, "Makanan & Minuman", 200_000)
    db.upsert_bill(CHAT, "Netflix", 54_000, 20, "langganan", TODAY)
    return db


# ---------- kalkulasi ----------
def test_summary_kpis_this_month(seeded):
    s = build_summary(seeded, CHAT, "bulan_ini", TODAY)
    k = s["kpi"]
    assert (k["income"], k["expense"], k["net"]) == (9_500_000, 645_000, 8_855_000)
    assert k["savings_rate"] == 93 and k["transactions"] == 5
    assert s["from"] == "2026-11-01" and s["to"] == "2026-11-17"  # tidak melewati hari ini


def test_summary_categories_sorted_with_percentages(seeded):
    s = build_summary(seeded, CHAT, "bulan_ini", TODAY)
    assert [(c["name"], c["total"]) for c in s["expense_by_category"]] == [
        ("Belanja", 500_000), ("Makanan & Minuman", 145_000)]
    assert sum(c["pct"] for c in s["expense_by_category"]) in (99, 100, 101)
    assert [(c["name"], c["total"]) for c in s["income_by_category"]] == [
        ("Gaji", 8_000_000), ("Freelance & Usaha", 1_500_000)]


def test_series_daily_for_short_ranges_and_fills_gaps(seeded):
    s = build_summary(seeded, CHAT, "bulan_ini", TODAY)
    assert s["granularity"] == "day" and len(s["series"]) == 17
    by_key = {p["key"]: p for p in s["series"]}
    assert by_key["2026-11-17"]["expense"] == 25_000
    assert by_key["2026-11-14"]["income"] == 1_500_000
    assert by_key["2026-11-16"] == {"key": "2026-11-16", "label": "16 Nov", "income": 0, "expense": 0}


def test_series_monthly_for_long_ranges(seeded):
    s = build_summary(seeded, CHAT, "3_bulan", TODAY)
    assert s["granularity"] == "month"
    assert [(p["key"], p["expense"]) for p in s["series"]] == [
        ("2026-09", 0), ("2026-10", 130_000), ("2026-11", 645_000)]
    assert s["series"][2]["label"] == "Nov 2026"


def test_all_time_starts_at_first_record(seeded):
    s = build_summary(seeded, CHAT, "semua", TODAY)
    assert s["from"] == d(40).isoformat()
    assert s["kpi"]["expense"] == 775_000


def test_empty_database_is_safe(db):
    s = build_summary(db, CHAT, "bulan_ini", TODAY)
    assert s["kpi"]["net"] == 0 and s["kpi"]["savings_rate"] is None
    assert s["expense_by_category"] == [] and s["top_expenses"] == [] and s["budgets"] == []
    assert build_summary(db, CHAT, "semua", TODAY)["series"]  # tetap ada bulan berjalan


def test_period_comparison(seeded):
    s = build_summary(seeded, CHAT, "bulan_lalu", TODAY)
    assert s["kpi"]["expense"] == 130_000 and s["kpi"]["expense_change"] is None  # tak ada pembanding
    seeded.add_expense(CHAT, 100_000, "x", "Belanja", date(2026, 9, 10))
    s = build_summary(seeded, CHAT, "bulan_lalu", TODAY)
    assert s["kpi"]["expense_change"] == 30  # 130k vs 100k


def test_budgets_bills_and_top_expenses(seeded):
    s = build_summary(seeded, CHAT, "bulan_ini", TODAY)
    assert s["budgets"] == [{"name": "Makanan & Minuman", "spent": 145_000, "limit": 200_000, "pct": 72,
                             "forecast": "2026-11-23"}]  # laju 8.500/hari: anggaran habis 23 Nov
    assert s["bills"]["items"][0]["name"] == "Netflix" and s["bills"]["items"][0]["days_left"] == 3
    assert s["bills"]["monthly_total"] == 54_000
    assert [t["note"] for t in s["top_expenses"]][:2] == ["sepatu", "makan malam"]


def test_summary_is_per_chat(seeded):
    s = build_summary(seeded, 2, "bulan_ini", TODAY)
    assert s["kpi"]["expense"] == 999_000 and s["kpi"]["income"] == 7_000_000
    assert s["budgets"] == [] and s["bills"]["items"] == []


def test_summary_rejects_unknown_period(db):
    with pytest.raises(ValueError):
        build_summary(db, CHAT, "kapan-kapan", TODAY)


# ---------- transaksi ----------
def test_transactions_filters_sort_and_paginate(seeded):
    r = list_transactions(seeded, CHAT, "semua", TODAY)
    assert r["total"] == 7 and r["rows"][0]["note"] == "kopi kenangan"  # terbaru dulu
    assert {x["kind"] for x in r["rows"]} == {"keluar", "masuk"}
    assert all("_k" not in x for x in r["rows"])
    assert list_transactions(seeded, CHAT, "semua", TODAY, kind="masuk")["total"] == 2
    assert list_transactions(seeded, CHAT, "semua", TODAY, category="makanan & minuman")["total"] == 3
    assert list_transactions(seeded, CHAT, "semua", TODAY, query="KOPI")["total"] == 2
    page2 = list_transactions(seeded, CHAT, "semua", TODAY, limit=3, offset=3)
    assert page2["limit"] == 3 and len(page2["rows"]) == 3 and page2["total"] == 7


def test_transactions_clamp_and_validate(seeded):
    assert list_transactions(seeded, CHAT, "semua", TODAY, limit=10_000)["limit"] == dashboard.MAX_ROWS
    assert list_transactions(seeded, CHAT, "semua", TODAY, limit=-5, offset=-9)["offset"] == 0
    with pytest.raises(ValueError):
        list_transactions(seeded, CHAT, "semua", TODAY, kind="aneh")
    with pytest.raises(ValueError):
        list_transactions(seeded, CHAT, "tak-ada", TODAY)


def test_transactions_per_chat(seeded):
    notes = {x["note"] for x in list_transactions(seeded, CHAT, "semua", TODAY, limit=100)["rows"]}
    assert "milik orang lain" not in notes and "gaji orang lain" not in notes


# ---------- uang aman & target tabungan ----------
def test_summary_includes_safe_daily_goals_and_budget_forecast(seeded):
    from keuangan.plans import PlanStore
    plans = PlanStore(seeded)
    plans.set_payday(CHAT, 25)
    g, _ = plans.create_goal(CHAT, "liburan", 5_000_000, date(2027, 3, 31), TODAY)
    plans.deposit(CHAT, g, 1_000_000, TODAY)
    s = build_summary(seeded, CHAT, "bulan_ini", TODAY)
    safe = s["safe_daily"]
    assert safe["mode"] == "pemasukan" and safe["days_left"] == 8 and safe["next_payday"] == "2026-11-25"
    # pemasukan 9.500.000 - pengeluaran siklus 735.000 (sejak 25 Okt) - Netflix 54.000 (jatuh tempo 20 Nov)
    # - tabungan 1.000.000
    assert safe["available"] == 7_711_000 and safe["per_day"] == 7_711_000 // 8 and safe["upcoming_bills"] == 54_000
    assert s["goals"] == [{"name": "liburan", "saved": 1_000_000, "target": 5_000_000, "pct": 20,
                           "deadline": "2027-03-31", "monthly": 1_000_000}]
    assert s["budgets"][0]["forecast"] == "2026-11-23"


def test_summary_without_basis_or_goals_is_null_safe(db):
    s = build_summary(db, CHAT, "bulan_ini", TODAY)
    assert s["safe_daily"] is None and s["goals"] == []


def test_summary_safe_daily_and_goals_are_per_user(seeded):
    from keuangan.plans import PlanStore
    PlanStore(seeded).create_goal(2, "rahasia siti", 1_000_000, None, TODAY)
    assert build_summary(seeded, CHAT, "bulan_ini", TODAY)["goals"] == []
    assert build_summary(seeded, 2, "bulan_ini", TODAY)["goals"][0]["name"] == "rahasia siti"
