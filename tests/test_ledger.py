import csv
import io
from datetime import date, timedelta

import pytest

from keuangan import ledger
from keuangan.parser import is_question, parse_date_text, parse_income
from keuangan.service import Service
from tests.conftest import CHAT, TODAY


@pytest.fixture
def svc(db):
    return Service(db, None, clock=lambda: TODAY)


# ---------- parser ----------
@pytest.mark.parametrize("text,amount,desc", [
    ("gaji 8jt", 8_000_000, "gaji"),
    ("+500rb freelance desain", 500_000, "freelance desain"),
    ("bonus 1,5jt", 1_500_000, "bonus"),
    ("Gaji November Rp 8.500.000", 8_500_000, "Gaji November"),
])
def test_parse_income(text, amount, desc):
    p = parse_income(text, TODAY)
    assert (p.amount, p.description) == (amount, desc)


@pytest.mark.parametrize("text", ["kopi 25rb", "terima kasih 50rb", "gaji", "makan siang 35rb"])
def test_parse_income_rejects_expenses_and_no_amount(text):
    assert parse_income(text, TODAY) is None


def test_parse_income_force_for_command():
    assert parse_income("freelance 2jt", TODAY, force=True).amount == 2_000_000


@pytest.mark.parametrize("text,expected", [
    ("kemarin", TODAY - timedelta(days=1)), ("3 hari lalu", TODAY - timedelta(days=3)),
    ("2026-11-05", date(2026, 11, 5)), ("5/11", date(2026, 11, 5)),
    ("30/12", date(2025, 12, 30)), ("05-11-2026", date(2026, 11, 5)),
    ("besok", None), ("2030-01-01", None), ("31/02", None), ("ngawur", None),
])
def test_parse_date_text(text, expected):
    assert parse_date_text(text, TODAY) == expected


@pytest.mark.parametrize("text,expected", [
    ("berapa pengeluaran kopi bulan ini?", True), ("bulan apa paling boros", True),
    ("boros nggak ya?", True), ("kopi 35rb", False), ("makan siang 35rb?", False),
    ("apalah 35rb", False),
])
def test_is_question(text, expected):
    assert is_question(text) is expected


# ---------- pemasukan & arus kas ----------
def test_income_flow_and_balance(svc):
    reply = svc.handle_text(CHAT, "gaji 8jt")
    assert "Pemasukan dicatat" in reply and "Rp8.000.000" in reply and "ID m1" in reply
    svc.handle_text(CHAT, "makan siang 35rb")
    saldo = svc.saldo_command(CHAT)
    assert "Rp8.000.000" in saldo and "Rp35.000" in saldo and "Rp7.965.000" in saldo
    assert "Ditabung: 99%" in saldo


def test_deficit_message(svc):
    svc.handle_text(CHAT, "+100rb")
    svc.handle_text(CHAT, "belanja 150rb")
    assert "🔴" in svc.saldo_command(CHAT) and "Defisit" in svc.saldo_command(CHAT)


def test_saldo_without_income_hints(svc):
    svc.handle_text(CHAT, "kopi 25rb")
    assert "Belum ada pemasukan" in svc.saldo_command(CHAT)


def test_income_command_variants(svc):
    assert "Format" in svc.income_command(CHAT, "tanpa nominal")
    assert "Rp2.000.000" in svc.income_command(CHAT, "freelance 2jt")


def test_income_not_counted_as_spending(svc, db):
    svc.handle_text(CHAT, "gaji 8jt")
    assert db.sum_between(CHAT, date(2026, 11, 1), date(2026, 12, 1)) == 0


def test_month_summary_includes_cashflow_only_with_income(svc):
    svc.handle_text(CHAT, "kopi 25rb")
    assert "Arus kas" not in svc.summary_command(CHAT, "bulan")
    svc.handle_text(CHAT, "gaji 8jt")
    assert "Arus kas bulan ini" in svc.summary_command(CHAT, "bulan")


def test_incomes_isolated_between_chats(svc, db):
    svc.handle_text(CHAT, "gaji 8jt")
    assert db.sum_income_between(2, date(2026, 1, 1), date(2027, 1, 1)) == 0


# ---------- riwayat / hapus / ubah ----------
def test_history_orders_by_entry_time_with_ids(svc):
    svc.handle_text(CHAT, "kopi 25rb")
    svc.handle_text(CHAT, "gaji 8jt")
    svc.handle_text(CHAT, "bensin 50rb")
    lines = svc.history_command(CHAT).splitlines()
    assert "bensin" in lines[1] and "gaji" in lines[2] and "kopi" in lines[3]
    assert "m1" in lines[2]


def test_history_empty(svc):
    assert svc.history_command(CHAT) == "Belum ada catatan."


def test_delete_by_id_and_latest_of_any_type(svc, db):
    svc.handle_text(CHAT, "kopi 25rb")
    svc.handle_text(CHAT, "gaji 8jt")
    assert "gaji" in svc.delete_command(CHAT)  # terakhir dicatat = pemasukan
    assert db.sum_income_between(CHAT, date(2026, 1, 1), date(2027, 1, 1)) == 0
    assert "kopi" in svc.delete_command(CHAT, "1")
    assert "tidak ditemukan" in svc.delete_command(CHAT, "99")
    assert "Format" in svc.delete_command(CHAT, "abc")


def test_delete_other_chats_entry_is_denied(svc, db):
    other = db.add_expense(2, 10_000, "rahasia", "Belanja", TODAY)
    assert "tidak ditemukan" in svc.delete_command(CHAT, str(other))
    assert db.get_expense(2, other) is not None


def test_edit_category_amount_date_note(svc, db):
    svc.handle_text(CHAT, "xyz 10rb")
    assert "Transportasi" in svc.edit_command(CHAT, "1 kategori transport")
    assert "Rp40.000" in svc.edit_command(CHAT, "1 nominal 40rb")
    assert "16 Nov" in svc.edit_command(CHAT, "1 tanggal kemarin")
    assert "makan malam" in svc.edit_command(CHAT, "1 catatan makan malam")
    e = db.get_expense(CHAT, 1)
    assert (e.category, e.amount, e.spent_on, e.description) == (
        "Transportasi", 40_000, date(2026, 11, 16), "makan malam")


def test_edit_income(svc, db):
    svc.handle_text(CHAT, "gaji 8jt")
    assert "Rp9.000.000" in svc.edit_command(CHAT, "m1 nominal 9jt")
    assert db.get_income(CHAT, 1).amount == 9_000_000
    assert "tidak punya kategori" in svc.edit_command(CHAT, "m1 kategori belanja")


@pytest.mark.parametrize("args", [
    "", "1", "1 kategori", "abc kategori belanja", "1 warna biru",
    "1 kategori ngawur", "1 nominal banyak", "1 tanggal besok",
])
def test_edit_rejects_bad_input_without_changing_data(svc, db, args):
    svc.handle_text(CHAT, "kopi 25rb")
    before = db.get_expense(CHAT, 1)
    reply = svc.edit_command(CHAT, args)
    assert reply and "Diubah" not in reply
    assert db.get_expense(CHAT, 1) == before


def test_edit_unknown_id_and_other_chat(svc, db):
    assert "tidak ditemukan" in svc.edit_command(CHAT, "99 nominal 10rb")
    other = db.add_expense(2, 10_000, "rahasia", "Belanja", TODAY)
    assert "tidak ditemukan" in svc.edit_command(CHAT, f"{other} nominal 99rb")
    assert db.get_expense(2, other).amount == 10_000


def test_db_update_rejects_unknown_columns(db):
    eid = db.add_expense(CHAT, 1000, "x", "Belanja", TODAY)
    with pytest.raises(ValueError):
        db.update_expense(CHAT, eid, source="x")  # kolom di luar daftar yang boleh diubah
    with pytest.raises(ValueError):
        db.update_expense(CHAT, eid, **{"amount=1, chat_id": 2})  # kunci berbahaya
    with pytest.raises(ValueError):
        db.update_expense(CHAT, eid)


# ---------- ekspor ----------
def parse_csv(data: bytes):
    assert data.startswith(b"\xef\xbb\xbf")
    return list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))


def test_export_current_month(svc):
    svc.handle_text(CHAT, "makan siang 35rb")
    svc.handle_text(CHAT, "gaji 8jt")
    svc.handle_text(CHAT, "kopi 25rb 30 hari lalu")  # bulan lalu: tidak ikut
    name, data = svc.export_command(CHAT)
    assert name == "keuangan-2026-11.csv"
    rows = parse_csv(data)
    assert [(r["jenis"], r["id"], r["jumlah"]) for r in rows] == [
        ("keluar", "1", "35000"), ("masuk", "m1", "8000000")]
    assert rows[1]["kategori"] == "Pemasukan"


def test_export_all_and_specific_month(svc):
    svc.handle_text(CHAT, "kopi 25rb 30 hari lalu")
    svc.handle_text(CHAT, "makan 35rb")
    assert len(parse_csv(svc.export_command(CHAT, "semua")[1])) == 2
    name, data = svc.export_command(CHAT, "2026-10")
    assert name == "keuangan-2026-10.csv" and len(parse_csv(data)) == 1


@pytest.mark.parametrize("arg", ["kemarin", "2026-13", "2026"])
def test_export_bad_period(svc, arg):
    assert isinstance(svc.export_command(CHAT, arg), str)


def test_export_neutralizes_formula_injection(db):
    db.add_expense(CHAT, 1000, "=HYPERLINK(\"http://evil\")", "Belanja", TODAY)
    db.add_expense(CHAT, 1000, "+cmd|' /C calc'!A0", "Belanja", TODAY)
    db.add_expense(CHAT, 1000, "-2+3", "Belanja", TODAY)
    db.add_expense(CHAT, 1000, "@SUM(1)", "Belanja", TODAY)
    db.add_expense(CHAT, 1000, "kopi, enak \"banget\"", "Belanja", TODAY)
    rows = parse_csv(ledger.export_csv(db, CHAT, date(2026, 11, 1), date(2026, 12, 1)))
    notes = [r["catatan"] for r in rows]
    assert all(n.startswith("'") for n in notes[:4])
    assert notes[4] == 'kopi, enak "banget"'  # koma & kutip aman lewat quoting CSV


def test_export_isolated_between_chats(svc, db):
    db.add_expense(2, 5000, "orang lain", "Belanja", TODAY)
    assert parse_csv(svc.export_command(CHAT)[1]) == []
