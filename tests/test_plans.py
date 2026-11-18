from datetime import date, timedelta

import pytest

from keuangan import plans
from keuangan.access import DELETE_CONFIRM
from keuangan.parser import find_amount, parse_expense, parse_income
from keuangan.plans import PlanStore, cycle, parse_deadline
from keuangan.service import Service
from tests.conftest import CHAT, TODAY  # TODAY = Selasa 17 Nov 2026

OTHER = 2


@pytest.fixture
def store(db):
    return PlanStore(db)


@pytest.fixture
def svc(db):
    return Service(db, None, clock=lambda: TODAY)


def d(n):
    return TODAY - timedelta(days=n)


# ---------- bahasa sehari-hari ----------
@pytest.mark.parametrize("text,amount", [
    ("kopi goceng", 5_000), ("parkir ceban", 10_000), ("bakso 2 goceng", 10_000),
    ("makan siang 3 ceban", 30_000), ("bensin gocap", 50_000), ("servis cepek", 100_000),
    ("kopi seceng", 1_000), ("kopi seribu", 1_000), ("laptop 2 sejuta", 2_000_000),
    ("GOCENG parkir", 5_000), ("kopi Goceng", 5_000),
])
def test_slang_amounts(text, amount):
    assert parse_expense(text, TODAY).amount == amount


def test_slang_works_for_income_and_alongside_normal_units():
    assert parse_income("bonus sejuta", TODAY).amount == 1_000_000
    assert find_amount("kopi 35rb")[0] == 35_000 and find_amount("makan 25k")[0] == 25_000
    assert find_amount("beli 2 kopi 50rb")[0] == 50_000      # angka kecil tetap tak dianggap nominal


@pytest.mark.parametrize("text", ["goceng2", "ngegoceng aja", "cebanyak", "sejutaan lebih"])
def test_slang_needs_word_boundaries(text):
    assert find_amount(text) is None


def test_slang_description_is_cleaned():
    p = parse_expense("2 goceng bakso", TODAY)
    assert (p.amount, p.description) == (10_000, "bakso")


# ---------- siklus gajian ----------
@pytest.mark.parametrize("today,payday,expected", [
    (date(2026, 11, 17), 25, (date(2026, 10, 25), date(2026, 11, 25))),
    (date(2026, 11, 25), 25, (date(2026, 11, 25), date(2026, 12, 25))),   # hari gajian: siklus baru
    (date(2026, 11, 26), 25, (date(2026, 11, 25), date(2026, 12, 25))),
    (date(2026, 12, 30), 25, (date(2026, 12, 25), date(2027, 1, 25))),     # lintas tahun
    (date(2027, 1, 5), 25, (date(2026, 12, 25), date(2027, 1, 25))),
    (date(2026, 11, 17), 1, (date(2026, 11, 1), date(2026, 12, 1))),
    (date(2027, 2, 10), 31, (date(2027, 1, 31), date(2027, 2, 28))),       # tanggal 31 di bulan pendek
    (date(2027, 2, 28), 31, (date(2027, 2, 28), date(2027, 3, 31))),
    (date(2028, 2, 29), 30, (date(2028, 2, 29), date(2028, 3, 30))),       # tahun kabisat
])
def test_cycle(today, payday, expected):
    assert cycle(today, payday) == expected


# ---------- uang aman per hari ----------
def test_no_basis_returns_none(db, store):
    assert plans.safe_daily(db, store, CHAT, TODAY) is None
    assert "belum punya dasar" in plans.format_safe_daily(None)


def test_income_mode_subtracts_spending_bills_and_savings(db, store):
    store.set_payday(CHAT, 25)
    db.add_income(CHAT, 8_000_000, "gaji", date(2026, 10, 25))
    db.add_expense(CHAT, 700_000, "belanja", "Belanja", d(3))
    db.upsert_bill(CHAT, "Listrik", 350_000, 20, "tagihan", TODAY)          # jatuh tempo 20 Nov < gajian 25 Nov
    db.upsert_bill(CHAT, "Netflix", 54_000, 28, "langganan", TODAY)         # 28 Nov: setelah gajian, tak dikurangi
    goal, _ = store.create_goal(CHAT, "liburan", 5_000_000, None, TODAY)
    store.deposit(CHAT, goal, 500_000, d(1))
    s = plans.safe_daily(db, store, CHAT, TODAY)
    assert s.mode == "pemasukan" and s.days_left == 8 and s.next_payday == date(2026, 11, 25)
    assert s.upcoming_bills == 350_000 and s.saved == 500_000
    assert s.available == 8_000_000 - 700_000 - 350_000 - 500_000 == 6_450_000
    assert s.per_day == 6_450_000 // 8


def test_budget_mode_uses_remaining_total_budget(db, store):
    store.set_payday(CHAT, 25)
    db.set_budget(CHAT, "*", 5_000_000)
    db.add_expense(CHAT, 1_000_000, "x", "Belanja", d(2))
    db.add_expense(CHAT, 400_000, "lama", "Belanja", date(2026, 10, 10))       # sebelum siklus: tak dihitung
    s = plans.safe_daily(db, store, CHAT, TODAY)
    assert s.mode == "anggaran" and s.available == 4_000_000 and s.per_day == 4_000_000 // 8


def test_income_before_cycle_start_does_not_count(db, store):
    store.set_payday(CHAT, 25)
    db.add_income(CHAT, 8_000_000, "gaji lama", date(2026, 10, 20))             # siklus sebelumnya
    assert plans.safe_daily(db, store, CHAT, TODAY) is None


def test_over_budget_and_pace_and_depletion(db, store):
    store.set_payday(CHAT, 25)
    db.set_budget(CHAT, "*", 1_000_000)
    for i in range(7):
        db.add_expense(CHAT, 140_000, "x", "Belanja", d(i))                     # 980rb dalam 7 hari
    s = plans.safe_daily(db, store, CHAT, TODAY)
    assert s.available == 20_000 and s.over_pace is True
    assert s.depletion == TODAY + timedelta(days=0)                              # 20rb / 140rb/hari < 1 hari
    db.add_expense(CHAT, 50_000, "lagi", "Belanja", TODAY)
    s = plans.safe_daily(db, store, CHAT, TODAY)
    assert s.available < 0 and s.per_day == 0
    text = plans.format_safe_daily(s)
    assert "melewati batas" in text and "kurang Rp30.000" in text


def test_pace_ok_and_no_depletion_before_payday(db, store):
    store.set_payday(CHAT, 25)
    db.add_income(CHAT, 8_000_000, "gaji", date(2026, 10, 25))
    db.add_expense(CHAT, 70_000, "kopi", "Makanan & Minuman", TODAY)
    s = plans.safe_daily(db, store, CHAT, TODAY)
    assert s.over_pace is False and s.depletion is None
    text = plans.format_safe_daily(s)
    assert "dalam batas aman" in text and "habis" not in text


def test_default_payday_is_noted(db, store):
    db.add_income(CHAT, 1_000_000, "gaji", date(2026, 11, 1))
    s = plans.safe_daily(db, store, CHAT, TODAY)
    assert s.payday_is_default and "/gajian" in plans.format_safe_daily(s)


def test_safe_daily_is_per_user(db, store):
    store.set_payday(CHAT, 25)
    db.add_income(CHAT, 8_000_000, "gaji", date(2026, 10, 25))
    db.add_expense(OTHER, 9_000_000, "besar", "Belanja", TODAY)
    assert plans.safe_daily(db, store, OTHER, TODAY) is None
    assert plans.safe_daily(db, store, CHAT, TODAY).available == 8_000_000


# ---------- prediksi anggaran ----------
def test_budget_forecast_predicts_depletion(db):
    db.set_budget(CHAT, "Makanan & Minuman", 200_000)
    db.add_expense(CHAT, 145_000, "makan", "Makanan & Minuman", d(0))
    f = plans.budget_forecasts(db, CHAT, TODAY)
    assert len(f) == 1 and f[0].depletion == date(2026, 11, 23) and f[0].daily_rate == 8_529
    assert "habis sekitar 23 Nov" in plans.format_forecasts(f)


def test_forecast_skips_safe_exhausted_and_early_month(db):
    db.set_budget(CHAT, "Belanja", 1_000_000)
    db.add_expense(CHAT, 100_000, "x", "Belanja", d(0))                          # laju rendah: tak habis bulan ini
    db.set_budget(CHAT, "Makanan & Minuman", 100_000)
    db.add_expense(CHAT, 150_000, "x", "Makanan & Minuman", d(0))                # sudah habis: ada peringatan 100%
    db.set_budget(CHAT, "Transportasi", 10_000)                                  # tanpa pengeluaran
    assert plans.budget_forecasts(db, CHAT, TODAY) == []
    early = date(2026, 11, 2)                                                    # baru 2 hari: terlalu dini
    assert plans.budget_forecasts(db, CHAT, early) == []


def test_forecast_is_per_user(db):
    db.set_budget(OTHER, "Belanja", 100_000)
    db.add_expense(OTHER, 90_000, "x", "Belanja", TODAY)
    assert plans.budget_forecasts(db, CHAT, TODAY) == []


# ---------- target tabungan ----------
def test_goal_create_update_and_progress(store):
    g, created = store.create_goal(CHAT, "Liburan Bali", 5_000_000, date(2027, 3, 31), TODAY)
    assert created and g.saved == 0 and g.pct == 0
    g2, created = store.create_goal(CHAT, "liburan bali", 6_000_000, None, TODAY)    # nama sama: diperbarui
    assert not created and g2.id == g.id and g2.target == 6_000_000 and g2.deadline is None
    g3 = store.deposit(CHAT, g2, 1_500_000, TODAY)
    assert g3.saved == 1_500_000 and g3.pct == 25 and g3.remaining == 4_500_000


def test_deposit_and_withdraw_rules(store):
    g, _ = store.create_goal(CHAT, "dana darurat", 10_000_000, None, TODAY)
    g = store.deposit(CHAT, g, 1_000_000, TODAY)
    assert store.deposit(CHAT, g, -2_000_000, TODAY) is None          # tak boleh melebihi saldo
    assert store.deposit(CHAT, g, 0, TODAY) is None
    g = store.deposit(CHAT, g, -400_000, TODAY)
    assert g.saved == 600_000
    over = store.deposit(CHAT, g, 20_000_000, TODAY)                   # melebihi target: tetap tercatat, persen dibatasi
    assert over.pct == 100 and over.remaining == 0


def test_find_goal_matching_and_isolation(store):
    store.create_goal(CHAT, "liburan bali", 5_000_000, None, TODAY)
    store.create_goal(OTHER, "rahasia orang lain", 1_000_000, None, TODAY)
    assert store.find_goal(CHAT, "LIBURAN BALI").name == "liburan bali"
    assert store.find_goal(CHAT, "bali").name == "liburan bali"
    assert store.find_goal(CHAT, "liburan bali ke ubud").name == "liburan bali"
    assert store.find_goal(CHAT, "rahasia") is None and store.find_goal(CHAT, "") is None
    other = store.find_goal(OTHER, "rahasia")
    assert store.deposit(CHAT, other, 100, TODAY) is None              # tak bisa menabung ke target orang lain
    assert store.delete_goal(CHAT, other.id) is False and store.find_goal(OTHER, "rahasia") is not None


def test_delete_goal_removes_deposits(db, store):
    g, _ = store.create_goal(CHAT, "x", 1000, None, TODAY)
    store.deposit(CHAT, g, 500, TODAY)
    assert store.delete_goal(CHAT, g.id) is True
    assert db.query("SELECT 1 FROM goal_deposits") == []


@pytest.mark.parametrize("text,expected", [
    ("2027-06-30", date(2027, 6, 30)), ("2027-06", date(2027, 6, 30)), ("06/2027", date(2027, 6, 30)),
    ("2027-02", date(2027, 2, 28)), ("2026-11-17", None), ("2026-11-18", date(2026, 11, 18)), ("2020-01", None), ("besok", None), ("2027-13", None),
])
def test_parse_deadline(text, expected):
    assert parse_deadline(text, TODAY) == expected


def test_monthly_suggestion_and_formatting(store):
    g, _ = store.create_goal(CHAT, "liburan", 5_000_000, date(2027, 3, 31), TODAY)
    assert plans.monthly_suggestion(g, TODAY) == 1_250_000                 # 4 bulan: Des, Jan, Feb, Mar
    text = plans.format_goal(g, TODAY)
    assert "0%" in text and "Rp1.250.000 per bulan" in text and "31 Mar 2027" in text
    done = store.deposit(CHAT, g, 5_000_000, TODAY)
    assert "Target tercapai" in plans.format_goal(done, TODAY)
    assert plans.monthly_suggestion(done, TODAY) is None
    assert "Belum ada target" in plans.format_goals([], TODAY)


# ---------- perintah lewat Service ----------
def test_gajian_command(svc):
    assert "belum diatur" in svc.gajian_command(CHAT)
    assert "Tanggal gajian: 25" in svc.gajian_command(CHAT, "25")
    assert "25" in svc.gajian_command(CHAT)
    for bad in ("0", "32", "abc", "-5", "25 26"):
        assert "Format" in svc.gajian_command(CHAT, bad), bad
    assert svc.plans.get_payday(CHAT) == 25


def test_aman_command_end_to_end(svc):
    assert "belum punya dasar" in svc.aman_command(CHAT)
    svc.handle_text(CHAT, "gaji 8jt")
    svc.gajian_command(CHAT, "25")
    out = svc.aman_command(CHAT)
    assert "per hari selama" in out and "25 Nov" in out


def test_target_command_create_list_delete(svc):
    assert "Belum ada target" in svc.target_command(CHAT)
    out = svc.target_command(CHAT, "liburan bali 5jt sampai 2027-03")
    assert "Target dibuat" in out and "Rp5.000.000" in out and "per bulan" in out
    assert "liburan bali" in svc.target_command(CHAT)
    assert "diperbarui" in svc.target_command(CHAT, "liburan bali 6jt")
    assert "tidak ditemukan" in svc.target_command(CHAT, "hapus tidakada")
    assert "dihapus" in svc.target_command(CHAT, "hapus liburan")
    assert "Belum ada target" in svc.target_command(CHAT)


@pytest.mark.parametrize("args", ["liburan", "5jt", "liburan 5jt sampai kapan-kapan", "liburan 5jt sampai 2020-01"])
def test_target_command_rejects_bad_input(svc, args):
    out = svc.target_command(CHAT, args)
    assert ("Format" in out or "Tenggat" in out) and svc.plans.list_goals(CHAT) == []


def test_target_limit_per_user(svc):
    for i in range(svc.MAX_GOALS):
        assert "dibuat" in svc.target_command(CHAT, f"target{i:02d} 1jt")
    assert "Maksimal" in svc.target_command(CHAT, "target baru 1jt")
    assert "diperbarui" in svc.target_command(CHAT, "target00 2jt")        # memperbarui yang ada tetap boleh
    assert "dibuat" in svc.target_command(OTHER, "target baru 1jt")        # batas per pengguna


def test_tabung_and_tarik_commands(svc):
    assert "Belum ada target" in svc.tabung_command(CHAT, "500rb")
    svc.target_command(CHAT, "liburan 5jt")
    assert "Ditabung Rp500.000" in svc.tabung_command(CHAT, "liburan 500rb")
    assert "Ditabung Rp100.000" in svc.tabung_command(CHAT, "100rb")          # satu target: nama boleh dihilangkan
    assert "Ditarik Rp200.000" in svc.tarik_command(CHAT, "liburan 200rb")
    assert svc.plans.list_goals(CHAT)[0].saved == 400_000
    assert "tidak cukup" in svc.tarik_command(CHAT, "liburan 9jt")
    assert "Format" in svc.tabung_command(CHAT, "liburan")
    svc.target_command(CHAT, "motor 20jt")
    assert "Target yang mana" in svc.tabung_command(CHAT, "500rb")            # dua target: nama wajib


def test_text_routing_for_saving_and_noun_conflict(svc, db):
    svc.target_command(CHAT, "liburan bali 5jt")
    assert "Ditabung Rp500.000" in svc.handle_text(CHAT, "nabung 500rb liburan")
    assert "Ditabung Rp250.000" in svc.handle_text(CHAT, "tabung liburan bali 250rb")
    out = svc.handle_text(CHAT, "tabung gas 150rb")                            # tabung = benda, bukan menabung
    assert "Dicatat" in out and "Rumah Tangga" in out
    assert db.sum_between(CHAT, TODAY, TODAY + timedelta(days=1)) == 150_000
    assert svc.plans.list_goals(CHAT)[0].saved == 750_000


def test_nabung_without_any_target_gives_hint(svc, db):
    out = svc.handle_text(CHAT, "nabung 500rb")
    assert "/target" in out and db.query("SELECT 1 FROM expenses") == []


def test_savings_reduce_safe_daily(svc):
    svc.handle_text(CHAT, "gaji 8jt")
    svc.gajian_command(CHAT, "25")
    before = svc.plans and plans.safe_daily(svc.db, svc.plans, CHAT, TODAY).available
    svc.target_command(CHAT, "liburan 5jt")
    svc.tabung_command(CHAT, "liburan 1jt")
    after = plans.safe_daily(svc.db, svc.plans, CHAT, TODAY).available
    assert before - after == 1_000_000


def test_budget_status_and_weekly_summary_show_forecast_and_safe_amount(svc):
    svc.handle_text(CHAT, "gaji 8jt")
    svc.gajian_command(CHAT, "25")
    svc.budget_command(CHAT, "makanan 200rb")
    svc.handle_text(CHAT, "makan 145rb 5 hari lalu")
    assert "anggaran habis sekitar" in svc.budget_command(CHAT, "")
    weekly = svc.weekly_job(CHAT)
    assert "Uang aman:" in weekly and "/aman" in weekly


# ---------- hak hapus & isolasi ----------
def test_account_deletion_removes_goals_deposits_and_settings(svc, db):
    svc.target_command(CHAT, "liburan 5jt")
    svc.tabung_command(CHAT, "liburan 1jt")
    svc.gajian_command(CHAT, "25")
    svc.target_command(OTHER, "punya orang lain 1jt")
    svc.access.delete_account(CHAT, DELETE_CONFIRM)
    for table in ("goals", "goal_deposits", "user_settings"):
        assert db.query(f"SELECT 1 FROM {table} WHERE chat_id=?", (CHAT,)) == [], table
    assert svc.plans.list_goals(OTHER)[0].name == "punya orang lain"


def test_commands_are_isolated_between_users(svc):
    svc.target_command(CHAT, "liburan 5jt")
    svc.tabung_command(CHAT, "liburan 1jt")
    assert "Belum ada target" in svc.target_command(OTHER)
    assert "Belum ada target" in svc.tabung_command(OTHER, "liburan 100rb")
    assert svc.plans.get_payday(OTHER) is None


def test_safe_daily_text_mentions_savings_and_has_no_contradiction(db, store):
    store.set_payday(CHAT, 25)
    db.add_income(CHAT, 8_000_000, "gaji", date(2026, 10, 25))
    g, _ = store.create_goal(CHAT, "dana darurat", 10_000_000, None, TODAY)
    store.deposit(CHAT, g, 9_000_000, TODAY)
    db.add_expense(CHAT, 50_000, "x", "Belanja", TODAY)
    text = plans.format_safe_daily(plans.safe_daily(db, store, CHAT, TODAY))
    assert "setoran tabungan siklus ini: Rp9.000.000" in text
    assert "melewati batas" in text and "dalam batas aman" not in text     # negatif: tak boleh ada "aman" hijau
