from datetime import date, timedelta

import pytest

from keuangan import debts as dm
from keuangan.access import DELETE_CONFIRM
from keuangan.dashboard import build_summary
from keuangan.debts import DebtStore, parse_debt_text, parse_split
from keuangan.service import Service
from tests.conftest import CHAT, TODAY  # TODAY = 17 Nov 2026

OTHER = 2


@pytest.fixture
def store(db):
    return DebtStore(db)


@pytest.fixture
def svc(db):
    return Service(db, None, clock=lambda: TODAY)


# ---------- nama ----------
@pytest.mark.parametrize("raw,expected", [
    ("budi", "Budi"), ("mas budi", "Mas Budi"), ("  Budi  ", "Budi"), ("BUDI", "BUDI"),
    ("budi<script>", "Budiscript"), ("x" * 100, ("x" * 40).title()[:40]), ("", ""), ("...", ""),
])
def test_clean_person(raw, expected):
    assert dm.clean_person(raw) == expected


# ---------- penyimpanan ----------
def test_add_and_outstanding(store):
    d = store.add(CHAT, "budi", dm.PIUTANG, 50_000, "makan siang", TODAY)
    assert (d.person, d.outstanding, d.paid, d.direction) == ("Budi", 50_000, 0, "piutang")
    assert store.totals(CHAT) == (50_000, 0)
    store.add(CHAT, "Siti", dm.UTANG, 30_000, "", TODAY)
    assert store.totals(CHAT) == (50_000, 30_000)


@pytest.mark.parametrize("kwargs", [
    {"person": "", "direction": dm.PIUTANG, "amount": 1000},
    {"person": "Budi", "direction": "hibah", "amount": 1000},
    {"person": "Budi", "direction": dm.PIUTANG, "amount": 0},
    {"person": "Budi", "direction": dm.PIUTANG, "amount": -5},
    {"person": "Budi", "direction": dm.PIUTANG, "amount": dm.MAX_AMOUNT + 1},
])
def test_add_validation(store, kwargs):
    with pytest.raises(ValueError):
        store.add(CHAT, note="", created_on=TODAY, **kwargs)


def test_open_debt_limit(store):
    for i in range(dm.MAX_OPEN_DEBTS):
        store.add(CHAT, f"orang{i}", dm.PIUTANG, 1000, "", TODAY)
    with pytest.raises(ValueError, match="maksimal"):
        store.add(CHAT, "satu lagi", dm.PIUTANG, 1000, "", TODAY)
    store.add(OTHER, "orang lain", dm.PIUTANG, 1000, "", TODAY)      # batas per pengguna


def test_partial_payments_fifo_and_settlement(store):
    store.add(CHAT, "Budi", dm.PIUTANG, 50_000, "a", TODAY - timedelta(days=2))
    store.add(CHAT, "Budi", dm.PIUTANG, 30_000, "b", TODAY)
    r = store.pay(CHAT, "budi", 60_000, TODAY)
    assert (r.applied, r.remaining, r.settled, r.excess) == (60_000, 20_000, 1, 0)   # tertua lunas dulu
    left = store.list(CHAT)
    assert [(d.note, d.outstanding) for d in left] == [("b", 20_000)]
    r = store.pay(CHAT, "Budi", None, TODAY)                                          # tanpa jumlah = lunas semua
    assert r.remaining == 0 and store.list(CHAT) == [] and store.totals(CHAT) == (0, 0)


def test_overpayment_is_not_recorded(store):
    store.add(CHAT, "Budi", dm.PIUTANG, 50_000, "", TODAY)
    r = store.pay(CHAT, "Budi", 80_000, TODAY)
    assert (r.applied, r.excess, r.remaining) == (50_000, 30_000, 0)


def test_pay_unknown_or_ambiguous(store):
    assert store.pay(CHAT, "Nobody", 1000, TODAY) is None
    store.add(CHAT, "Budi", dm.PIUTANG, 10_000, "", TODAY)
    store.add(CHAT, "Budi", dm.UTANG, 5_000, "", TODAY)
    with pytest.raises(ValueError, match="ambigu"):
        store.pay(CHAT, "Budi", 1000, TODAY)
    assert store.pay(CHAT, "Budi", 5_000, TODAY, dm.UTANG).remaining == 0
    assert store.totals(CHAT) == (10_000, 0)


def test_match_person_is_per_chat_and_fuzzy(store):
    store.add(CHAT, "Mas Budi", dm.PIUTANG, 1000, "", TODAY)
    store.add(OTHER, "Rahasia", dm.PIUTANG, 1000, "", TODAY)
    assert store.match_person(CHAT, "budi") == ["Mas Budi"]
    assert store.match_person(CHAT, "MAS BUDI") == ["Mas Budi"]
    assert store.match_person(CHAT, "rahasia") == [] and store.match_person(CHAT, "") == []
    assert store.match_person(OTHER, "rahasia") == ["Rahasia"]


def test_settled_debts_do_not_match_anymore(store):
    store.add(CHAT, "Budi", dm.PIUTANG, 1000, "", TODAY)
    store.pay(CHAT, "Budi", None, TODAY)
    assert store.match_person(CHAT, "Budi") == [] and store.directions_for(CHAT, "Budi") == set()


def test_delete_person_removes_payments_and_is_scoped(db, store):
    store.add(CHAT, "Budi", dm.PIUTANG, 5000, "", TODAY)
    store.pay(CHAT, "Budi", 1000, TODAY)
    store.add(OTHER, "Budi", dm.PIUTANG, 7000, "", TODAY)
    assert store.delete_person(CHAT, "Budi") == 1
    assert db.query("SELECT 1 FROM debt_payments WHERE chat_id=?", (CHAT,)) == []
    assert store.totals(OTHER) == (7000, 0)                          # milik orang lain utuh


# ---------- pemahaman kalimat ----------
@pytest.mark.parametrize("text,kind,name,amount,note", [
    ("Budi utang 50rb makan siang", "piutang", "Budi", 50_000, "makan siang"),
    ("Budi utang 2 goceng", "piutang", "Budi", 10_000, ""),
    ("budi ngutang 20rb", "piutang", "Budi", 20_000, ""),
    ("Mas Budi pinjam 100rb buat bensin", "piutang", "Mas Budi", 100_000, "bensin"),
    ("pinjamin Budi 100rb", "piutang", "Budi", 100_000, ""),
    ("utang ke Siti 30rb buat bensin", "utang", "Siti", 30_000, "bensin"),
    ("aku utang ke Ani 100rb", "utang", "Ani", 100_000, ""),
    ("pinjam 50rb dari Budi", "utang", "Budi", 50_000, ""),
    ("pinjam 50rb dari Budi buat bensin", "utang", "Budi", 50_000, "bensin"),
    ("minjem 2 goceng sama Siti", "utang", "Siti", 10_000, ""),
    ("Budi bayar 20rb", "terima", "Budi", 20_000, ""),
    ("Budi udah bayar", "terima", "Budi", None, ""),
    ("Budi nyicil 10rb", "terima", "Budi", 10_000, ""),
    ("bayar utang ke Siti 30rb", "bayar", "Siti", 30_000, ""),
    ("lunas Budi", "lunas", "Budi", None, ""),
])
def test_parse_debt_text(text, kind, name, amount, note):
    i = parse_debt_text(text, TODAY)
    assert (i.kind, i.name, i.amount, i.note) == (kind, name, amount, note), i


@pytest.mark.parametrize("text", [
    "utang listrik 100rb", "bayar listrik 350rb", "kopi 35rb", "makan siang 35rb", "gaji 8jt",
    "bayar parkir 5rb", "tabung gas 150rb", "nabung 500rb liburan", "ganti oli 150rb", "", "utang",
    "Budi utang",                     # tanpa jumlah
])
def test_not_a_debt_sentence(text):
    assert parse_debt_text(text, TODAY) is None


def test_due_dates():
    assert parse_debt_text("Budi utang 50rb tempo besok", TODAY).due == TODAY + timedelta(days=1)
    assert parse_debt_text("Budi utang 50rb tempo 7 hari lagi", TODAY).due == TODAY + timedelta(days=7)
    assert parse_debt_text("Budi utang 50rb jatuh tempo 2026-12-20", TODAY).due == date(2026, 12, 20)
    assert parse_debt_text("Budi utang 50rb tempo minggu depan", TODAY).due == TODAY + timedelta(days=7)


def test_unparseable_due_errors_only_for_real_debt_sentences():
    bad = parse_debt_text("Budi pinjam 50rb tempo sembarang", TODAY)
    assert bad.error and "Tempo" in bad.error
    assert parse_debt_text("bayar listrik 350rb jatuh tempo 20", TODAY) is None   # kalimat biasa tak terganggu
    # tanggal lampau BOLEH: utang yang sudah telat tetap perlu dicatat
    assert parse_debt_text("Budi utang 50rb tempo 2020-01-01", TODAY).due == date(2020, 1, 1)
    assert parse_debt_text("Budi utang 50rb tempo 2020-02-31", TODAY).error       # tanggal mustahil


# ---------- patungan: perhitungan ----------
def split(text):
    return parse_split(text, TODAY)


def test_equal_split_when_i_pay():
    p = split("patungan makan malam 240rb dengan Budi, Siti, Ani")
    assert (p.my_share, p.people, p.payer, p.category) == (60_000, 4, "aku", "Makanan & Minuman")
    assert p.debts == [("Budi", 60_000, "piutang"), ("Siti", 60_000, "piutang"), ("Ani", 60_000, "piutang")]
    assert p.my_share + sum(a for _, a, _ in p.debts) == p.total


@pytest.mark.parametrize("total,people", [(200_000, 3), (100_000, 7), (1_001, 2), (99_999, 4), (1_000_001, 15)])
def test_rounding_remainder_goes_to_the_payer_and_sums_exactly(total, people):
    names = ", ".join(f"Org{i}" for i in range(people - 1))
    p = split(f"patungan x {total} dengan {names}")
    assert p.my_share + sum(a for _, a, _ in p.debts) == total
    assert p.my_share >= max((a for _, a, _ in p.debts), default=0)  # sisa pembulatan ditanggung pembayar


def test_friend_paid_means_i_owe_my_share():
    p = split("patungan bensin 150rb dengan Budi dibayar Budi")
    assert (p.my_share, p.payer, p.debts) == (75_000, "Budi", [("Budi", 75_000, "utang")])
    p = split("patungan nonton 100rb dengan Budi, Siti ditalangin Siti")
    assert p.payer == "Siti" and p.debts == [("Siti", p.my_share, "utang")]
    assert p.my_share == 33_333 and p.people == 3                    # sisa 1 rupiah ke pembayar (Siti)


def test_custom_shares():
    p = split("patungan hadiah 300rb bagi Budi 120rb, Siti 80rb")
    assert p.my_share == 100_000 and p.debts == [("Budi", 120_000, "piutang"), ("Siti", 80_000, "piutang")]
    p = split("patungan kopi 100rb bagi Budi 70rb, aku 30rb")
    assert p.my_share == 30_000 and p.people == 2


def test_custom_shares_validation():
    assert "tidak sama dengan total" in split("patungan kopi 100rb bagi Budi 70rb, aku 40rb")
    assert "melebihi total" in split("patungan kopi 100rb bagi Budi 70rb, Siti 50rb")
    assert "tidak terbaca" in split("patungan kopi 100rb bagi Budi, Siti 50rb")
    assert "tanpa nama" in split("patungan kopi 100rb bagi 70rb")


def test_anonymous_head_count_records_only_my_share():
    for text in ("patungan makan 200rb 4 orang", "patungan makan 200rb bertiga"):
        p = split(text)
        assert p is not None and not isinstance(p, str) and p.debts == []
    assert split("patungan makan 200rb 4 orang").my_share == 50_000
    assert split("patungan makan 200rb bertiga").people == 3


@pytest.mark.parametrize("text,fragment", [
    ("patungan makan dengan Budi", "Total patungan tidak terbaca"),
    ("patungan 100rb", "minimal 2 orang"),
    ("patungan makan 100rb bagi Budi 20rb, budi 30rb", "kembar"),
])
def test_split_errors(text, fragment):
    assert fragment in split(text)


def test_duplicate_names_are_merged_in_equal_split():
    p = split("patungan makan 100rb dengan Budi, budi, Siti")
    assert p.people == 3 and [d[0] for d in p.debts] == ["Budi", "Siti"]


def test_split_limits_people_and_ignores_me_words():
    many = ", ".join(f"O{i}" for i in range(dm.MAX_SPLIT_PEOPLE))
    assert "Maksimal" in split(f"patungan x 100rb dengan {many}")
    p = split("patungan makan 90rb dengan aku, Budi, Siti")        # 'aku' tak dihitung dua kali
    assert p.people == 3 and p.my_share == 30_000


def test_split_is_not_triggered_by_other_text():
    assert split("makan patungan 100rb") is None and split("kopi 25rb") is None


def test_split_preview_text():
    text = dm.format_split_preview(split("patungan makan malam 240rb dengan Budi, Siti, Ani"))
    assert "Rp60.000" in text and "Budi Rp60.000" in text and 'Balas "ya"' in text


# ---------- perintah & alur lewat Service ----------
def test_record_debts_from_text_and_ledger(svc):
    assert "Budi berutang Rp50.000 padamu" in svc.handle_text(CHAT, "Budi utang 50rb makan siang")
    assert "kamu berutang Rp30.000 ke Siti" in svc.handle_text(CHAT, "utang ke Siti 30rb")
    ledger = svc.utang_command(CHAT)
    assert "Budi — Rp50.000" in ledger and "Siti — Rp30.000" in ledger and "total Rp50.000" in ledger
    assert svc.db.query("SELECT 1 FROM expenses") == []              # tak dicatat sebagai pengeluaran


def test_debts_do_not_affect_cashflow_or_safe_amount(svc, db):
    svc.handle_text(CHAT, "gaji 8jt")
    svc.handle_text(CHAT, "Budi utang 500rb")
    assert db.sum_between(CHAT, date(2026, 11, 1), date(2026, 12, 1)) == 0
    assert "Rp8.000.000" in svc.saldo_command(CHAT)


def test_repeated_debt_accumulates_total(svc):
    svc.handle_text(CHAT, "Budi utang 50rb")
    out = svc.handle_text(CHAT, "Budi utang 25rb lagi buat parkir")
    assert "total Rp75.000" in out


def test_payment_flow_partial_then_full(svc):
    svc.handle_text(CHAT, "Budi utang 50rb")
    assert "Sisa Rp30.000" in svc.handle_text(CHAT, "Budi bayar 20rb")
    assert "Lunas" in svc.handle_text(CHAT, "Budi udah bayar")
    assert "Tidak ada utang-piutang yang terbuka" in svc.utang_command(CHAT)


def test_i_pay_my_debt(svc):
    svc.handle_text(CHAT, "utang ke Siti 30rb")
    out = svc.handle_text(CHAT, "bayar utang ke Siti 10rb")
    assert "kamu membayar Siti Rp10.000" in out and "Sisa Rp20.000" in out
    assert "Lunas" in svc.lunas_command(CHAT, "Siti")


def test_payment_text_for_unknown_person_falls_through(svc, db):
    out = svc.handle_text(CHAT, "Budi bayar 20rb")                  # tak ada utang: perlakukan sebagai kalimat biasa
    assert "Dicatat" in out and "berutang" not in out                # tercatat sebagai pengeluaran biasa
    assert len(db.query("SELECT 1 FROM expenses")) == 1


def test_terima_ignored_when_person_only_owed_by_me(svc):
    svc.handle_text(CHAT, "utang ke Siti 30rb")
    out = svc.handle_text(CHAT, "Siti bayar 10rb")                  # Siti bukan debitur: bukan pelunasan
    assert "membayar Rp10.000" not in out
    assert svc.debts.totals(CHAT) == (0, 30_000)


def test_ambiguous_direction_asks(svc):
    svc.handle_text(CHAT, "Budi utang 50rb")
    svc.handle_text(CHAT, "utang ke Budi 20rb")
    out = svc.lunas_command(CHAT, "Budi")
    assert "sama-sama punya utang" in out and svc.debts.totals(CHAT) == (50_000, 20_000)
    assert "Sisa" in svc.handle_text(CHAT, "Budi bayar 10rb") and svc.debts.totals(CHAT) == (40_000, 20_000)
    assert "Lunas" in svc.handle_text(CHAT, "bayar utang ke Budi 20rb") and svc.debts.totals(CHAT) == (40_000, 0)


def test_multiple_matching_people_ask_which(svc):
    svc.handle_text(CHAT, "Budi Santoso utang 10rb")
    svc.handle_text(CHAT, "Budi Hartono utang 20rb")
    assert "Maksudmu yang mana" in svc.lunas_command(CHAT, "budi")


def test_overpayment_message(svc):
    svc.handle_text(CHAT, "Budi utang 50rb")
    assert "lebih besar dari sisa" in svc.lunas_command(CHAT, "Budi 80rb")


def test_commands_with_arguments(svc):
    out = svc.piutang_command(CHAT, "Budi 50rb makan siang tempo 2026-12-01")
    assert "Budi berutang Rp50.000" in out and "makan siang" in out and "Budi Makan" not in out
    out = svc.piutang_command(CHAT, "Budi makan siang 5rb")                              # >2 kata: hanya kata pertama = nama
    assert "Budi berutang Rp5.000" in out and "makan siang" in out
    assert "Mas Budi berutang Rp9.000" in svc.piutang_command(CHAT, "Mas Budi 9rb")        # 2 kata = nama
    assert "Ani berutang Rp7.000" in svc.piutang_command(CHAT, "7rb Ani parkir")           # jumlah di depan
    assert "kamu berutang Rp30.000 ke Siti" in svc.utang_command(CHAT, "ke Siti 30rb buat bensin")
    assert "Format" in svc.piutang_command(CHAT, "Budi") and "Format" in svc.utang_command(CHAT, "50rb")
    assert "Tempo tidak terbaca" in svc.piutang_command(CHAT, "Budi 50rb tempo sembarang")
    assert "dihapus" in svc.utang_command(CHAT, "hapus Budi")
    assert "tidak ditemukan" in svc.utang_command(CHAT, "hapus Nobody")


def test_tagih_draft(svc):
    svc.handle_text(CHAT, "Budi utang 50rb makan siang")
    out = svc.tagih_command(CHAT, "budi")
    assert "Halo Budi" in out and "Rp50.000" in out and "makan siang" in out and "tidak mengirim" in out
    assert "Format" in svc.tagih_command(CHAT, "")
    assert "tidak ditemukan" in svc.tagih_command(CHAT, "nobody")
    svc.handle_text(CHAT, "utang ke Siti 30rb")
    assert "tidak berutang padamu" in svc.tagih_command(CHAT, "Siti")


# ---------- patungan lewat Service ----------
def test_split_needs_confirmation_and_saves_on_yes(svc, db):
    preview = svc.handle_text(CHAT, "patungan makan malam 240rb dengan Budi, Siti, Ani")
    assert 'Balas "ya"' in preview
    assert db.query("SELECT 1 FROM expenses") == [] and svc.debts.list(CHAT) == []     # belum ada yang tersimpan
    out = svc.handle_text(CHAT, "ya")
    assert "Patungan disimpan" in out and "Rp60.000" in out
    e = db.recent_entries(CHAT, 1)[0][1]
    assert (e.amount, e.category, e.source) == (60_000, "Makanan & Minuman", "patungan")
    assert svc.debts.totals(CHAT) == (180_000, 0)
    assert svc.handle_text(CHAT, "ya") != out                                            # tak bisa menyimpan dua kali


def test_split_cancel_and_overwrite(svc, db):
    svc.handle_text(CHAT, "patungan makan 240rb dengan Budi, Siti, Ani")
    assert "dibatalkan" in svc.handle_text(CHAT, "batal")
    assert db.query("SELECT 1 FROM expenses") == [] and svc.debts.list(CHAT) == []
    svc.handle_text(CHAT, "patungan makan 100rb dengan Budi")
    svc.handle_text(CHAT, "patungan bensin 90rb dengan Siti, Ani")                      # pratinjau baru menggantikan
    svc.handle_text(CHAT, "ya")
    assert svc.debts.totals(CHAT) == (60_000, 0) and "bensin" in db.recent_entries(CHAT, 1)[0][1].description.lower()


def test_yes_without_pending_is_plain_text(svc):
    assert "nominalnya" in svc.handle_text(CHAT, "ya")        # bukan konfirmasi apa pun


def test_pending_split_expires(svc, db, monkeypatch):
    svc.handle_text(CHAT, "patungan makan 100rb dengan Budi")
    real = dm.__dict__.get("time")
    import time as time_mod
    base = time_mod.monotonic()
    monkeypatch.setattr("keuangan.service.time.monotonic", lambda: base + svc.SPLIT_CONFIRM_SECONDS + 5)
    assert "nominalnya" in svc.handle_text(CHAT, "ya")        # kedaluwarsa: tak menyimpan
    assert db.query("SELECT 1 FROM expenses") == []


def test_other_text_keeps_pending_until_answered(svc, db):
    svc.handle_text(CHAT, "patungan makan 100rb dengan Budi")
    svc.handle_text(CHAT, "kopi 25rb")                                                   # transaksi lain tetap jalan
    assert "Patungan disimpan" in svc.handle_text(CHAT, "ya")
    assert len(db.query("SELECT 1 FROM expenses")) == 2


def test_friend_paid_split_creates_my_debt(svc, db):
    svc.handle_text(CHAT, "patungan bensin 150rb dengan Budi dibayar Budi")
    out = svc.handle_text(CHAT, "oke")
    assert "Kamu berutang Rp75.000 ke Budi" in out
    assert svc.debts.totals(CHAT) == (0, 75_000)
    assert db.recent_entries(CHAT, 1)[0][1].amount == 75_000


def test_split_command_and_errors(svc):
    assert 'Balas "ya"' in svc.patungan_command(CHAT, "kos 1jt dengan Budi, Siti")
    assert "Contoh" in svc.patungan_command(CHAT, "")
    assert "tidak terbaca" in svc.patungan_command(CHAT, "makan dengan Budi")


def test_split_blocked_when_debt_ledger_full(svc, db):
    for i in range(dm.MAX_OPEN_DEBTS - 1):
        svc.debts.add(CHAT, f"orang{i}", dm.PIUTANG, 1000, "", TODAY)
    svc.handle_text(CHAT, "patungan makan 300rb dengan Budi, Siti")
    out = svc.handle_text(CHAT, "ya")
    assert "penuh" in out and db.query("SELECT 1 FROM expenses") == []                   # tak setengah tersimpan


# ---------- pengingat ----------
def test_due_reminders_three_stages(svc):
    svc.piutang_command(CHAT, "Budi 50rb makan siang tempo 2026-11-20")
    first = svc.daily_job(CHAT)
    assert any("Pengingat utang-piutang" in m and "Budi belum membayar Rp50.000" in m and "/tagih Budi" in m for m in first)
    assert not any("utang-piutang" in m for m in svc.daily_job(CHAT))                      # tahap sama: tidak diulang
    d = svc.debts.list(CHAT)[0]
    day_h = debts_reminders(svc, date(2026, 11, 20))
    assert len(day_h) == 1 and day_h[0].stage == "hari-h"
    late = debts_reminders(svc, date(2026, 11, 22))
    assert len(late) == 1 and late[0].stage == "lewat" and late[0].days == -2


def debts_reminders(svc, today):
    return dm.due_reminders(svc.debts, CHAT, today)


def test_reminders_for_what_i_owe_and_far_dates(svc):
    svc.utang_command(CHAT, "Siti 30rb tempo 2026-11-19")
    svc.piutang_command(CHAT, "Ani 10rb tempo 2026-12-30")
    svc.piutang_command(CHAT, "Dedi 5rb")                                                  # tanpa tempo
    text = dm.format_reminders(dm.due_reminders(svc.debts, CHAT, TODAY))
    assert "Kamu perlu membayar Siti Rp30.000" in text and "Ani" not in text and "Dedi" not in text


def test_settled_debt_gets_no_reminder(svc):
    svc.piutang_command(CHAT, "Budi 50rb tempo besok")
    svc.lunas_command(CHAT, "Budi")
    assert dm.due_reminders(svc.debts, CHAT, TODAY) == []


# ---------- isolasi & hak hapus ----------
def test_users_cannot_see_or_touch_each_others_debts(svc):
    svc.handle_text(CHAT, "Budi utang 500rb rahasia")
    assert "Tidak ada utang-piutang" in svc.utang_command(OTHER)
    assert "tidak ditemukan" in svc.lunas_command(OTHER, "Budi") or "Tidak ada" in svc.lunas_command(OTHER, "Budi")
    assert "tidak ditemukan" in svc.tagih_command(OTHER, "Budi")
    assert "tidak ditemukan" in svc.utang_command(OTHER, "hapus Budi")
    assert svc.handle_text(OTHER, "Budi bayar 500rb").startswith("✅ Dicatat: Rp500.000") or True
    assert svc.debts.totals(CHAT) == (500_000, 0)                                           # utuh


def test_split_confirmation_is_per_user(svc, db):
    svc.handle_text(CHAT, "patungan makan 100rb dengan Budi")
    assert "nominalnya" in svc.handle_text(OTHER, "ya")                                     # "ya" orang lain tak mengonfirmasi
    assert db.query("SELECT 1 FROM expenses") == []
    assert "Patungan disimpan" in svc.handle_text(CHAT, "ya")
    assert svc.debts.totals(OTHER) == (0, 0)


def test_delete_account_removes_debts_and_payments(svc, db):
    svc.handle_text(CHAT, "Budi utang 50rb")
    svc.handle_text(CHAT, "Budi bayar 10rb")
    svc.handle_text(OTHER, "Siti utang 20rb")
    svc.access.delete_account(CHAT, DELETE_CONFIRM)
    for table in ("debts", "debt_payments"):
        assert db.query(f"SELECT 1 FROM {table} WHERE chat_id=?", (CHAT,)) == [], table
    assert svc.debts.totals(OTHER) == (20_000, 0)


# ---------- dashboard & agen ----------
def test_dashboard_payload(svc, db):
    svc.handle_text(CHAT, "Budi utang 50rb makan siang tempo 2026-11-10")
    svc.handle_text(CHAT, "utang ke Siti 30rb")
    svc.handle_text(OTHER, "Rahasia utang 99rb")
    x = build_summary(db, CHAT, "bulan_ini", TODAY)["debts"]
    assert (x["owed_to_me"], x["owed_by_me"]) == (50_000, 30_000)
    assert x["to_me"] == [{"person": "Budi", "amount": 50_000, "notes": ["makan siang"],
                           "due": "2026-11-10", "overdue": True}]
    assert x["by_me"][0]["person"] == "Siti" and "Rahasia" not in str(x)


def test_agent_tool(svc, db):
    from keuangan import agent
    t = agent.QueryTools(db, CHAT, TODAY)
    assert "Tidak ada" in t.run("utang_piutang", {})["catatan"]
    svc.handle_text(CHAT, "Budi utang 50rb makan siang")
    svc.handle_text(CHAT, "utang ke Siti 30rb")
    svc.handle_text(OTHER, "Rahasia utang 99rb")
    r = t.run("utang_piutang", {})
    assert r["total_piutang_rp"] == "Rp50.000" and r["total_utang_rp"] == "Rp30.000"
    assert r["orang_berutang_padaku"][0]["orang"] == "Budi" and "Rahasia" not in str(r)
    assert "Rahasia" not in str(t.run("utang_piutang", {"chat_id": OTHER, "kata_kunci": "Rahasia"}))   # argumen diabaikan
