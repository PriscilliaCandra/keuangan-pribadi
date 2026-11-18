from datetime import date, timedelta

import pytest

from keuangan.llm import LlmError, extract_json
from keuangan.service import Service, receipt_to_expense
from tests.conftest import CHAT, TODAY


class FakeLlm:
    def __init__(self, category=None, receipt=None, error=False):
        self.category, self.receipt, self.error = category, receipt, error

    def categorize(self, description):
        if self.error:
            raise LlmError("x")
        return self.category

    def read_receipt(self, image, media_type):
        if self.error:
            raise LlmError("x")
        return self.receipt


@pytest.fixture
def svc(db):
    return Service(db, None, clock=lambda: TODAY)


def test_handle_text_saves_and_confirms(svc, db):
    reply = svc.handle_text(CHAT, "makan siang 35rb")
    assert "Rp35.000" in reply and "Makanan & Minuman" in reply
    assert db.sum_between(CHAT, date(2026, 11, 1), date(2026, 12, 1)) == 35_000


def test_handle_text_without_amount(svc, db):
    assert "tidak menemukan nominal" in svc.handle_text(CHAT, "halo")
    assert db.sum_between(CHAT, date(2026, 1, 1), date(2027, 1, 1)) == 0


def test_unknown_category_falls_back_to_lainnya_without_llm(svc, db):
    assert "Lainnya" in svc.handle_text(CHAT, "xyz 10rb")


def test_unknown_category_uses_llm(db):
    s = Service(db, FakeLlm(category="Kesehatan"), clock=lambda: TODAY)
    assert "Kesehatan" in s.handle_text(CHAT, "xyz 10rb")


def test_llm_failure_does_not_block_saving(db):
    s = Service(db, FakeLlm(error=True), clock=lambda: TODAY)
    assert "Lainnya" in s.handle_text(CHAT, "xyz 10rb")


def test_budget_alert_appears_in_confirmation(svc):
    svc.budget_command(CHAT, "makanan 100rb")
    reply = svc.handle_text(CHAT, "makan malam 85rb")
    assert "⚠️" in reply and "85%" in reply


def test_old_expense_does_not_trigger_current_month_alert(svc):
    svc.budget_command(CHAT, "makanan 100rb")
    reply = svc.handle_text(CHAT, "makan 95rb 30 hari lalu")
    assert "⚠️" not in reply and "🚨" not in reply


def test_undo(svc):
    assert "Belum ada" in svc.delete_command(CHAT)
    svc.handle_text(CHAT, "kopi 25rb")
    assert "Dihapus" in svc.delete_command(CHAT)
    assert "Belum ada" in svc.delete_command(CHAT)


def test_budget_command_variants(svc):
    assert "Belum ada anggaran" in svc.budget_command(CHAT, "")
    assert "Rp1.500.000" in svc.budget_command(CHAT, "makanan 1,5jt")
    assert "total bulanan" in svc.budget_command(CHAT, "total 5jt")
    listing = svc.budget_command(CHAT, "")
    assert "Makanan & Minuman" in listing and "total bulanan" in listing
    assert "dihapus" in svc.budget_command(CHAT, "hapus makanan")
    assert "Kategori tidak dikenal" in svc.budget_command(CHAT, "ngawur 1jt")
    assert "Format" in svc.budget_command(CHAT, "makanan")


def test_summary_command(svc):
    svc.handle_text(CHAT, "makan siang 35rb")
    svc.budget_command(CHAT, "total 1jt")
    week = svc.summary_command(CHAT, "")
    assert "Rp35.000" in week and "Anggaran bulan ini" in week
    assert "Ringkasan bulan ini" in svc.summary_command(CHAT, "bulan")


def test_bills_flow(svc, db):
    assert "Belum ada" in svc.bills_command(CHAT, "tagihan", "")
    assert "Ditambahkan" in svc.bills_command(CHAT, "langganan", "netflix 54rb tgl 5")
    assert "Diperbarui" in svc.bills_command(CHAT, "langganan", "netflix 65rb tgl 5")
    assert "Format" in svc.bills_command(CHAT, "tagihan", "listrik")
    assert "Netflix".lower() in svc.bills_command(CHAT, "tagihan", "").lower()
    assert "berhenti" in svc.cancel_command(CHAT, "netf")
    assert db.list_bills(CHAT) == []
    assert "tidak ditemukan" in svc.cancel_command(CHAT, "zzz")


def test_daily_job_reminds_once_per_stage(svc):
    svc.bills_command(CHAT, "tagihan", "listrik 350rb tgl 20")
    first = svc.daily_job(CHAT)
    assert len(first) == 1 and "listrik" in first[0] and "3 hari lagi" in first[0]
    assert svc.daily_job(CHAT) == []


def test_daily_job_monthly_review_on_first(db):
    day1 = date(2026, 12, 1)
    s = Service(db, None, clock=lambda: day1)
    db.upsert_bill(CHAT, "Netflix", 54_000, 20, "langganan", day1 - timedelta(days=120))
    out = s.daily_job(CHAT)
    assert any("masih dipakai" in m.lower() for m in out)


def test_scan_email_disabled_message(svc):
    assert "belum aktif" in svc.scan_email(CHAT)[0]


# ---------- struk ----------
def good(**kw):
    return {"is_receipt": True, "merchant": "Indomaret", "total": 85000,
            "date": "2026-11-16", "category": "Belanja", **kw}


def test_receipt_valid():
    p = receipt_to_expense(good(), TODAY)
    assert (p.amount, p.description, p.category, p.spent_on) == (85_000, "Indomaret", "Belanja", date(2026, 11, 16))


@pytest.mark.parametrize("bad", [
    None, {}, good(is_receipt=False), good(total=0), good(total=-5), good(total="abc"),
    good(total=True), good(total=5_000_000_000),
])
def test_receipt_rejects_bad_data(bad):
    assert receipt_to_expense(bad, TODAY) is None


def test_receipt_accepts_numeric_strings_from_small_models():
    assert receipt_to_expense(good(total="Rp 85.000"), TODAY).amount == 85_000


def test_receipt_sanitizes_date_and_category():
    p = receipt_to_expense(good(date="2030-01-01", category="Hack"), TODAY)
    assert p.spent_on == TODAY and p.category == "Belanja"  # tebak dari 'Indomaret'
    assert receipt_to_expense(good(date="kemarin"), TODAY).spent_on == TODAY


def test_read_receipt_paths(db):
    no_llm = Service(db, None, clock=lambda: TODAY)
    assert no_llm.read_receipt(b"x")[0] is None
    ok = Service(db, FakeLlm(receipt=good()), clock=lambda: TODAY)
    parsed, msg = ok.read_receipt(b"x")
    assert parsed.amount == 85_000 and "Simpan?" in msg
    assert Service(db, FakeLlm(receipt={"is_receipt": False}), clock=lambda: TODAY).read_receipt(b"x")[0] is None
    assert Service(db, FakeLlm(error=True), clock=lambda: TODAY).read_receipt(b"x")[0] is None


def test_extract_json_tolerates_wrapping():
    assert extract_json('Ini hasilnya: {"a": 1} selesai') == {"a": 1}
    assert extract_json("tidak ada json") is None
    assert extract_json("{rusak") is None
    assert extract_json("[1, 2]") is None


def test_scan_email_rejects_concurrent_runs(db):
    import threading
    started, release = threading.Event(), threading.Event()

    def slow_fetch():
        started.set()
        release.wait(5)
        return []

    s = Service(db, FakeLlm(), clock=lambda: TODAY, email_fetcher=slow_fetch)
    first = []
    t = threading.Thread(target=lambda: first.append(s.scan_email(CHAT)))
    t.start()
    assert started.wait(5)
    assert "sedang berjalan" in s.scan_email(CHAT)[0]
    release.set()
    t.join()
    assert first == [[]]


def test_dashboard_command(db):
    s = Service(db, None, clock=lambda: TODAY)
    assert "tidak aktif" in s.dashboard_command(CHAT)
    s.dashboard_url = "http://localhost:8765/?t=abc"
    out = s.dashboard_command(CHAT)
    assert "http://localhost:8765/?t=abc" in out and "Jangan bagikan" in out
