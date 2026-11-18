from datetime import date, timedelta

import pytest

from keuangan import bills
from tests.conftest import CHAT, TODAY


def add(db, name="Listrik", amount=350_000, day=20, kind="tagihan", cycle="monthly", month=None):
    return db.upsert_bill(CHAT, name, amount, day, kind, TODAY, cycle, month)[0]


def test_next_due_this_month_and_rollover(db):
    assert bills.next_due(add(db, day=20), TODAY) == date(2026, 11, 20)
    assert bills.next_due(add(db, "Air", day=5), TODAY) == date(2026, 12, 5)


def test_next_due_december_rolls_to_january(db):
    assert bills.next_due(add(db, day=5), date(2026, 12, 20)) == date(2027, 1, 5)


def test_day_31_clamped_in_short_month(db):
    assert bills.next_due(add(db, day=31), date(2027, 2, 10)) == date(2027, 2, 28)


def test_yearly_due(db):
    b = add(db, "Pajak motor", 2_000_000, 10, "tagihan", "yearly", 3)
    assert bills.next_due(b, TODAY) == date(2027, 3, 10)
    assert bills.next_due(b, date(2027, 3, 10)) == date(2027, 3, 10)


def test_two_stage_reminders(db):
    b = add(db, day=20)
    early = bills.due_reminders(db, CHAT, TODAY, days_ahead=3)
    assert len(early) == 1 and early[0].days_left == 3
    db.mark_reminded(b.id, early[0].token)
    assert bills.due_reminders(db, CHAT, TODAY, 3) == []  # tidak diulang
    day_h = bills.due_reminders(db, CHAT, date(2026, 11, 20), 3)
    assert len(day_h) == 1 and day_h[0].days_left == 0  # tahap hari-H


def test_reminder_window_excludes_far_bills(db):
    add(db, day=28)
    assert bills.due_reminders(db, CHAT, TODAY, 3) == []


def test_inactive_bills_not_reminded(db):
    b = add(db, day=18)
    db.set_bill_active(b.id, False)
    assert bills.due_reminders(db, CHAT, TODAY, 3) == []


def test_stale_subscriptions(db):
    s = db.upsert_bill(CHAT, "Netflix", 54_000, 5, "langganan", TODAY - timedelta(days=100))[0]
    db.upsert_bill(CHAT, "Listrik", 1, 5, "tagihan", TODAY - timedelta(days=100))
    assert [b.name for b in bills.stale_subscriptions(db, CHAT, TODAY)] == ["Netflix"]
    db.confirm_bill(s.id, TODAY)
    assert bills.stale_subscriptions(db, CHAT, TODAY) == []


def test_email_update_does_not_reactivate_cancelled(db):
    b = add(db, "Netflix", 54_000, 5, "langganan")
    db.set_bill_active(b.id, False)
    again, old, created = db.upsert_bill(CHAT, "Netflix", 65_000, 5, "langganan", TODAY, source="email")
    assert not created and old == 54_000 and again.amount == 65_000 and not again.active
    manual = db.upsert_bill(CHAT, "Netflix", 65_000, 5, "langganan", TODAY, source="manual")[0]
    assert manual.active


def test_detect_recurring_consecutive_months(db):
    for m in (8, 9, 10, 11):
        db.add_expense(CHAT, 54_000, "Netflix", "Langganan & Hiburan", date(2026, m, 5))
    # kebiasaan harian bukan langganan
    for d in (1, 2, 3):
        db.add_expense(CHAT, 25_000, "kopi", "Makanan & Minuman", date(2026, 11, d))
    assert bills.detect_recurring(db, CHAT, TODAY) == [("Netflix", 54_000)]


def test_detect_recurring_skips_known_and_gaps(db):
    for m in (6, 8, 10):  # bolong
        db.add_expense(CHAT, 54_000, "Spotify", "Langganan & Hiburan", date(2026, m, 5))
    assert bills.detect_recurring(db, CHAT, TODAY) == []
    for m in (8, 9, 10):
        db.add_expense(CHAT, 54_000, "Netflix", "Langganan & Hiburan", date(2026, m, 5))
    add(db, "Netflix", 54_000, 5, "langganan")
    assert bills.detect_recurring(db, CHAT, TODAY) == []


@pytest.mark.parametrize("text,expected", [
    ("netflix 54rb tgl 5", ("netflix", 54_000, 5, "monthly", None)),
    ("listrik 350rb 20", ("listrik", 350_000, 20, "monthly", None)),
    ("pajak motor 2jt tgl 10 bulan 3 tahunan", ("pajak motor", 2_000_000, 10, "yearly", 3)),
    ("wifi tanggal 1", ("wifi", None, 1, "monthly", None)),
])
def test_parse_bill_args(text, expected):
    p = bills.parse_bill_args(text)
    assert (p.name, p.amount, p.due_day, p.cycle, p.due_month) == expected


@pytest.mark.parametrize("text", ["", "netflix 54rb", "tgl 5", "netflix tgl 40", "pajak 1jt tgl 5 tahunan"])
def test_parse_bill_args_invalid(text):
    assert bills.parse_bill_args(text) is None
