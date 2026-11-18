from datetime import date

from keuangan import budget
from keuangan.formatting import rupiah
from tests.conftest import CHAT, TODAY


def spend(db, amount, category="Makanan & Minuman", d=TODAY):
    db.add_expense(CHAT, amount, "x", category, d)


def test_month_bounds_december():
    assert budget.month_bounds(date(2026, 12, 15)) == (date(2026, 12, 1), date(2027, 1, 1))


def test_no_alert_without_budget(db):
    spend(db, 500_000)
    assert budget.check_alerts(db, CHAT, "Makanan & Minuman", TODAY) == []


def test_warns_at_80_then_100_each_once(db):
    db.set_budget(CHAT, "Makanan & Minuman", 1_000_000)
    spend(db, 700_000)
    assert budget.check_alerts(db, CHAT, "Makanan & Minuman", TODAY) == []
    spend(db, 150_000)  # 85%
    first = budget.check_alerts(db, CHAT, "Makanan & Minuman", TODAY)
    assert len(first) == 1 and "85%" in first[0]
    spend(db, 10_000)  # 86%: tidak diulang
    assert budget.check_alerts(db, CHAT, "Makanan & Minuman", TODAY) == []
    spend(db, 200_000)  # 106%
    over = budget.check_alerts(db, CHAT, "Makanan & Minuman", TODAY)
    assert len(over) == 1 and "habis" in over[0] and rupiah(60_000) in over[0]
    assert budget.check_alerts(db, CHAT, "Makanan & Minuman", TODAY) == []


def test_total_budget_counts_all_categories(db):
    db.set_budget(CHAT, budget.TOTAL, 1_000_000)
    spend(db, 500_000, "Belanja")
    spend(db, 400_000, "Transportasi")
    msgs = budget.check_alerts(db, CHAT, "Transportasi", TODAY)
    assert len(msgs) == 1 and "total bulanan" in msgs[0]


def test_alert_resets_next_month(db):
    db.set_budget(CHAT, "Belanja", 100_000)
    spend(db, 90_000, "Belanja")
    assert budget.check_alerts(db, CHAT, "Belanja", TODAY)
    dec = date(2026, 12, 3)
    spend(db, 90_000, "Belanja", dec)
    assert budget.check_alerts(db, CHAT, "Belanja", dec)


def test_other_months_not_counted(db):
    db.set_budget(CHAT, "Belanja", 100_000)
    spend(db, 500_000, "Belanja", date(2026, 10, 30))
    assert budget.statuses(db, CHAT, TODAY)[0].spent == 0


def test_changing_limit_rearms_alert(db):
    db.set_budget(CHAT, "Belanja", 100_000)
    spend(db, 90_000, "Belanja")
    assert budget.check_alerts(db, CHAT, "Belanja", TODAY)
    db.set_budget(CHAT, "Belanja", 95_000)
    assert budget.check_alerts(db, CHAT, "Belanja", TODAY)


def test_tenant_isolation(db):
    db.set_budget(CHAT, "Belanja", 100_000)
    db.add_expense(2, 90_000, "x", "Belanja", TODAY)
    assert budget.statuses(db, CHAT, TODAY)[0].spent == 0
