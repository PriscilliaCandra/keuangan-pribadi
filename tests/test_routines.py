import csv
import io
import zipfile
from datetime import date

import pytest

from keuangan import routines as rt
from keuangan.access import DELETE_CONFIRM
from keuangan.agent import QueryTools
from keuangan.dashboard import build_summary
from keuangan.export import export_zip
from keuangan.routines import RecurringStore, parse_recurring_args, recurring_args_from_text
from keuangan.service import Service
from tests.conftest import CHAT, TODAY  # TODAY = 17 Nov 2026

OTHER = 2


@pytest.fixture
def store(db):
    return RecurringStore(db)


@pytest.fixture
def svc(db):
    return Service(db, None, clock=lambda: TODAY)


def svc_on(db, day):
    return Service(db, None, clock=lambda: day)


# ---------- pemahaman perintah ----------
@pytest.mark.parametrize("args,expected", [
    ("kos 1,5jt tgl 1", ("kos", 1_500_000, 1, "keluar")),
    ("gaji 8jt tgl 25 masuk", ("gaji", 8_000_000, 25, "masuk")),
    ("cicilan motor 850rb tanggal 10", ("cicilan motor", 850_000, 10, "keluar")),
    ("gaji 8jt tgl 25", ("gaji", 8_000_000, 25, "masuk")),
])
def test_parse_ok(args, expected):
    spec = parse_recurring_args(args)
    assert (spec.name, spec.amount, spec.day, spec.kind) == expected


@pytest.mark.parametrize("args", ["kos 1jt", "kos tgl 1", "tgl 1 1jt", "netflix 50rb tgl 32", "", "kos 1jt tgl 0"])
def test_parse_errors_are_messages(args):
    assert isinstance(parse_recurring_args(args), str)


def test_text_trigger_needs_amount_and_every():
    assert recurring_args_from_text("kos 1,5jt tiap bulan tgl 1")
    assert recurring_args_from_text("netflix 54rb setiap tanggal 5")
    assert recurring_args_from_text("berapa pengeluaran tiap bulan?") is None
    assert recurring_args_from_text("kopi 25rb") is None
    assert recurring_args_from_text("kos tiap bulan") is None


# ---------- penyimpanan & jadwal ----------
def test_add_future_day_is_due_later(store):
    item, created = store.add(CHAT, "Kos", "keluar", 1_500_000, "Tagihan & Utilitas", 25, TODAY)
    assert created and item.last_run_month is None
    assert store.due(CHAT, TODAY) == []
    assert [r.name for r, _ in store.due(CHAT, date(2026, 11, 25))] == ["Kos"]


def test_add_past_day_not_double_counted_this_month(store):
    item, _ = store.add(CHAT, "Kos", "keluar", 1_500_000, "Tagihan & Utilitas", 1, TODAY)
    assert item.last_run_month == "2026-11"
    assert store.due(CHAT, TODAY) == []
    assert len(store.due(CHAT, date(2026, 12, 1))) == 1


def test_claim_is_atomic_per_month(store):
    item, _ = store.add(CHAT, "Kos", "keluar", 1, "X", 25, TODAY)
    d = date(2026, 11, 25)
    assert store.claim(CHAT, item.id, d) is True
    assert store.claim(CHAT, item.id, d) is False
    assert store.claim(CHAT, item.id, date(2026, 12, 25)) is True


def test_day_31_clamped_in_short_month(store):
    store.add(CHAT, "Gaji", "masuk", 5, "Gaji", 31, date(2027, 2, 1))
    assert store.due(CHAT, date(2027, 2, 27)) == []
    assert [d for _, d in store.due(CHAT, date(2027, 2, 28))] == [date(2027, 2, 28)]


def test_update_same_name_and_limit(store):
    store.add(CHAT, "Kos", "keluar", 1, "X", 25, TODAY)
    item, created = store.add(CHAT, "kos", "keluar", 2, "X", 26, TODAY)
    assert not created and item.amount == 2 and len(store.list(CHAT)) == 1
    for i in range(rt.MAX_RECURRING - 1):
        store.add(CHAT, f"n{i}", "keluar", 1, "X", 5, TODAY)
    with pytest.raises(ValueError):
        store.add(CHAT, "terlalu banyak", "keluar", 1, "X", 5, TODAY)


def test_isolation_between_users(store):
    item, _ = store.add(CHAT, "Kos", "keluar", 1, "X", 25, TODAY)
    assert store.list(OTHER) == [] and store.find(OTHER, "kos") is None
    assert store.delete(OTHER, item.id) is False
    assert store.set_active(OTHER, item.id, False) is False
    assert store.claim(OTHER, item.id, date(2026, 11, 25)) is False
    assert len(store.list(CHAT)) == 1


# ---------- lewat Service ----------
def test_command_and_text_creation(svc, db):
    assert "dicatat otomatis tiap tgl 25" in svc.handle_text(CHAT, "cicilan hp 400rb tiap bulan tgl 25")
    assert "Belum ada" in svc.rutin_command(OTHER)
    assert "cicilan hp" in svc.rutin_command(CHAT)


def test_question_with_every_month_is_not_a_routine(db):
    svc = Service(db, None, clock=lambda: TODAY)
    svc.handle_text(CHAT, "kopi 25rb")
    assert RecurringStore(db).list(CHAT) == []


def test_daily_job_records_once(db):
    svc = svc_on(db, TODAY)
    svc.rutin_command(CHAT, "cicilan 400rb tgl 25")
    svc.rutin_command(CHAT, "gaji 8jt tgl 25 masuk")
    on25 = svc_on(db, date(2026, 11, 25))
    first = on25.daily_job(CHAT)
    assert sum("Rutin otomatis" in m for m in first) == 2
    assert on25.daily_job(CHAT) == []
    assert db.sum_between(CHAT, date(2026, 11, 1), date(2026, 12, 1)) == 400_000
    assert db.sum_income_between(CHAT, date(2026, 11, 1), date(2026, 12, 1)) == 8_000_000
    assert len(svc_on(db, date(2026, 12, 25)).daily_job(CHAT)) == 2  # bulan depan jalan lagi


def test_missed_day_catches_up_and_paused_skipped(db):
    svc = svc_on(db, TODAY)
    svc.rutin_command(CHAT, "kos 1jt tgl 20")
    svc.rutin_command(CHAT, "gym 200rb tgl 20")
    svc.rutin_command(CHAT, "jeda gym")
    late = svc_on(db, date(2026, 11, 28))  # laptop mati tanggal 20: tetap tercatat dengan tanggal 20
    msgs = late.daily_job(CHAT)
    assert len(msgs) == 1 and "kos" in msgs[0]
    assert [e.spent_on for e in db.expenses_between(CHAT, date(2026, 11, 1), date(2026, 12, 1))] == [date(2026, 11, 20)]


def test_pause_resume_delete(svc):
    svc.rutin_command(CHAT, "kos 1jt tgl 25")
    assert "dijeda" in svc.rutin_command(CHAT, "jeda kos")
    assert "dijeda" in svc.rutin_command(CHAT)
    assert "dilanjutkan" in svc.rutin_command(CHAT, "lanjut kos")
    assert "dihapus" in svc.rutin_command(CHAT, "hapus kos")
    assert "tidak ditemukan" in svc.rutin_command(CHAT, "hapus kos")


# ---------- pengingat malam ----------
def test_evening_reminder_rules(db):
    svc = svc_on(db, TODAY)
    assert "mati" in svc.ingatkan_command(CHAT)
    assert "aktif" in svc.ingatkan_command(CHAT, "on")
    assert svc.plans.evening_chat_ids() == {CHAT}
    assert svc.evening_job(CHAT) == []  # belum pernah mencatat: jangan mengganggu
    db.execute("INSERT INTO expenses (chat_id, amount, description, category, spent_on, created_at)"
               " VALUES (?,?,?,?,?,?)", (CHAT, 1000, "x", "Lainnya", "2026-11-10", "2026-11-10 05:00:00"))
    assert len(svc.evening_job(CHAT)) == 1  # aktif tapi hari ini belum mencatat
    svc.handle_text(CHAT, "kopi 25rb")
    assert svc.evening_job(CHAT) == []
    assert "dimatikan" in svc.ingatkan_command(CHAT, "off")
    assert svc.plans.evening_chat_ids() == set()
    assert svc.plans.get_evening(OTHER) is False


# ---------- ekspor semua ----------
def _zip_rows(blob, name):
    data = zipfile.ZipFile(io.BytesIO(blob)).read(name)
    assert data.startswith(b"\xef\xbb\xbf")
    return list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))


def test_export_zip_contains_everything_and_only_own(db, svc):
    svc.handle_text(CHAT, "makan 35rb")
    svc.handle_text(CHAT, "Budi utang 50rb makan")
    svc.handle_text(CHAT, "Budi bayar 20rb")
    svc.target_command(CHAT, "liburan 5jt sampai 2027-03")
    svc.tabung_command(CHAT, "liburan 500rb")
    svc.budget_command(CHAT, "makanan 1jt")
    svc.rutin_command(CHAT, "kos 1jt tgl 25")
    svc.gajian_command(CHAT, "25")
    svc.handle_text(OTHER, "rahasia 99rb")
    svc.handle_text(OTHER, "Dedi utang 77rb")
    name, blob = svc.export_command(CHAT, "semua")
    assert name == "keuangan-semua.zip"
    names = set(zipfile.ZipFile(io.BytesIO(blob)).namelist())
    assert {"transaksi.csv", "anggaran.csv", "tagihan_langganan.csv", "target_tabungan.csv",
            "setoran_tabungan.csv", "utang_piutang.csv", "pembayaran_utang.csv", "catatan_rutin.csv",
            "pengaturan.csv", "BACA-SAYA.txt"} <= names
    assert [r["jumlah"] for r in _zip_rows(blob, "transaksi.csv")] == ["35000"]
    assert [(r["orang"], r["jumlah"]) for r in _zip_rows(blob, "utang_piutang.csv")] == [("Budi", "50000")]
    assert [r["jumlah"] for r in _zip_rows(blob, "pembayaran_utang.csv")] == ["20000"]
    assert [r["jumlah"] for r in _zip_rows(blob, "setoran_tabungan.csv")] == ["500000"]
    assert [r["nama"] for r in _zip_rows(blob, "catatan_rutin.csv")] == ["kos"]
    assert {r["pengaturan"]: r["nilai"] for r in _zip_rows(blob, "pengaturan.csv")}["tanggal_gajian"] == "25"
    everything = b"".join(zipfile.ZipFile(io.BytesIO(blob)).read(n) for n in names)
    assert b"rahasia" not in everything and b"Dedi" not in everything


def test_export_zip_blocks_csv_formula_injection(db):
    RecurringStore(db).add(CHAT, "=HYPERLINK(1)", "keluar", 5, "X", 5, TODAY)
    rows = _zip_rows(export_zip(db, CHAT, TODAY), "catatan_rutin.csv")
    assert rows[0]["nama"].startswith("'=")


def test_export_zip_empty_account(db):
    blob = export_zip(db, CHAT, TODAY)
    assert _zip_rows(blob, "transaksi.csv") == []


# ---------- dashboard, agen, hapus akun ----------
def test_dashboard_and_agent_see_only_own(db, svc):
    svc.rutin_command(CHAT, "kos 1jt tgl 25")
    svc.rutin_command(OTHER, "rahasia 9jt tgl 3")
    s = build_summary(db, CHAT, "bulan_ini", TODAY)
    assert [r["name"] for r in s["recurring"]] == ["kos"]
    out = QueryTools(db, CHAT, TODAY).run("catatan_rutin", {"apa": "saja"})
    assert [r["nama"] for r in out["rutin"]] == ["kos"]
    assert "Belum ada" in QueryTools(db, 99, TODAY).run("catatan_rutin", {})["catatan"]


def test_delete_account_removes_recurring_and_evening(db, svc):
    svc.rutin_command(CHAT, "kos 1jt tgl 25")
    svc.ingatkan_command(CHAT, "on")
    svc.rutin_command(OTHER, "gaji 1jt tgl 3 masuk")
    svc.access.delete_account(CHAT, DELETE_CONFIRM)
    assert db.query("SELECT 1 FROM recurring WHERE chat_id=?", (CHAT,)) == []
    assert db.query("SELECT 1 FROM user_settings WHERE chat_id=?", (CHAT,)) == []
    assert len(RecurringStore(db).list(OTHER)) == 1
