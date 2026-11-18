from datetime import date, timedelta

import pytest

from keuangan.dashboard import build_summary
from keuangan.insights import build_insights, format_insights
from keuangan.service import Service

CHAT, OTHER = 1, 2
TODAY = date(2026, 11, 18)


@pytest.fixture
def svc(db):
    return Service(db, None, clock=lambda: TODAY)


def add(db, chat, amount, desc, cat, day, source="teks"):
    db.add_expense(chat, amount, desc, cat, day, source)


def kinds(db, chat=CHAT, today=TODAY):
    return {i.kind: i for i in build_insights(db, chat, today)}


def test_not_enough_data_is_empty(db):
    add(db, CHAT, 10_000, "kopi", "Makanan", TODAY)
    assert build_insights(db, CHAT, TODAY) == []
    assert "belum cukup" in format_insights([]).lower()


def test_category_increase_vs_same_span_last_month(db):
    for d in (2, 5, 9):
        add(db, CHAT, 100_000, "ojek", "Transportasi", date(2026, 10, d))
    for d in (2, 5, 9, 12, 15):
        add(db, CHAT, 100_000, "ojek", "Transportasi", date(2026, 11, d))
    add(db, CHAT, 900_000, "belanja besar", "Belanja", date(2026, 10, 25))  # di luar periode sama: diabaikan
    k = kinds(db)
    assert "naik" in k and "Transportasi naik 67%" in k["naik"].text


def test_category_decrease(db):
    for d in (2, 4, 6, 8, 10):
        add(db, CHAT, 100_000, "nongkrong", "Hiburan", date(2026, 10, d))
    add(db, CHAT, 100_000, "nongkrong", "Hiburan", date(2026, 11, 3))
    for d in (1, 2, 3, 4):
        add(db, CHAT, 5_000, "parkir", "Transportasi", date(2026, 11, d))
    assert "turun" in kinds(db)


def test_small_frequent_habit_with_yearly_projection(db):
    for d in range(1, 9):
        add(db, CHAT, 25_000, "kopi susu", "Makanan", date(2026, 11, d))
    habit = kinds(db)["kebiasaan"]
    assert "kopi" in habit.text and "8x" in habit.text and "Rp2.400.000" in habit.text


def test_routine_entries_are_not_habits(db):
    for d in range(1, 8):
        add(db, CHAT, 25_000, "kos", "Tagihan", date(2026, 11, d), source="rutin")
    assert "kebiasaan" not in kinds(db)


def test_weekend_heavier(db):
    day = TODAY - timedelta(days=29)
    for i in range(30):
        d = day + timedelta(days=i)
        add(db, CHAT, 150_000 if d.weekday() >= 5 else 20_000, "jajan", "Makanan", d)
    assert "akhir_pekan" in kinds(db)


def test_projection_over_last_month(db):
    add(db, CHAT, 1_000_000, "bulan lalu", "Belanja", date(2026, 10, 20))
    for d in range(1, 9):
        add(db, CHAT, 200_000, "belanja", "Belanja", date(2026, 11, d))
    assert "proyeksi" in kinds(db)


def test_deficit_warning(db):
    db.add_income(CHAT, 500_000, "gaji", date(2026, 11, 1), "teks", "Gaji")
    for d in range(1, 7):
        add(db, CHAT, 200_000, "belanja", "Belanja", date(2026, 11, d))
    assert "defisit" in kinds(db)


def test_max_four_sorted_by_impact(db):
    db.add_income(CHAT, 100_000, "gaji", date(2026, 11, 1), "teks", "Gaji")
    for d in range(1, 9):
        add(db, CHAT, 25_000, "kopi", "Makanan", date(2026, 11, d))
        add(db, CHAT, 300_000, "belanja", "Belanja", date(2026, 11, d))
    add(db, CHAT, 100_000, "x", "Belanja", date(2026, 10, 3))
    items = build_insights(db, CHAT, TODAY)
    assert len(items) <= 4
    assert [i.weight for i in items] == sorted((i.weight for i in items), reverse=True)


def test_other_users_data_never_used(db):
    for d in range(1, 9):
        add(db, OTHER, 25_000, "rahasia", "Makanan", date(2026, 11, d))
    assert build_insights(db, CHAT, TODAY) == []
    assert "rahasia" in format_insights(build_insights(db, OTHER, TODAY))


def test_command_weekly_and_dashboard(db, svc):
    for d in range(1, 9):
        add(db, CHAT, 25_000, "kopi", "Makanan", date(2026, 11, d))
    assert "Insight" in svc.insight_command(CHAT)
    assert "kopi" in svc.weekly_job(CHAT)
    assert any("kopi" in t for t in build_summary(db, CHAT, "bulan_ini", TODAY)["insights"])
    assert build_summary(db, OTHER, "bulan_ini", TODAY)["insights"] == []
