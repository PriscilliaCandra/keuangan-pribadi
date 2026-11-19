from datetime import date

import pytest

from keuangan.reports import parse_detail_request
from keuangan.service import Service
from tests.conftest import CHAT, TODAY

OTHER = 2


@pytest.mark.parametrize("text,title", [
    ("jelaskan detail pengeluaranku selama ini", "semua waktu"),
    ("rincian pengeluaran bulan ini", "bulan ini"),
    ("tolong rangkum pengeluaran bulan lalu", "bulan lalu"),
    ("jelaskan pengeluaran minggu ini dong", "7 hari"),
    ("ceritakan keuanganku", "bulan ini"),
    ("detail pengeluaran hari ini", "hari ini"),
    ("lihat pengeluaran tahun ini", "tahun ini"),
])
def test_general_detail_requests_are_recognised(text, title):
    assert parse_detail_request(text, TODAY)[2] == title


@pytest.mark.parametrize("text", [
    "detail pengeluaran kopi", "lihat pengeluaran terbesar", "cek saldo", "kopi 25rb",
    "lihat pengeluaran transportasi", "berapa pengeluaran bulan ini", "cek kesehatan 200rb", "",
])
def test_specific_or_other_requests_fall_through(text):
    assert parse_detail_request(text, TODAY) is None


def test_ranges():
    start, end, _ = parse_detail_request("rincian pengeluaran bulan lalu", date(2026, 1, 15))
    assert (start, end) == (date(2025, 12, 1), date(2026, 1, 1))
    assert parse_detail_request("rincian pengeluaran minggu ini", TODAY)[0] == date(2026, 11, 11)


def test_service_answers_without_ai_and_only_own_data(db):
    svc = Service(db, None, clock=lambda: TODAY)
    svc.handle_text(CHAT, "makan siang 35rb")
    svc.handle_text(CHAT, "TLOGOMAS 75400")
    svc.handle_text(OTHER, "rahasia orang lain 999rb")
    out = svc.handle_text(CHAT, "jelaskan detail pengeluaranku selama ini")
    assert "Rincian pengeluaran semua waktu" in out and "Rp110.400" in out and "2 transaksi" in out
    assert "TLOGOMAS" in out and "Terbesar:" in out
    assert "rahasia" not in out and "999" not in out


def test_empty_account(db):
    svc = Service(db, None, clock=lambda: TODAY)
    assert "Belum ada pengeluaran" in svc.handle_text(CHAT, "rincian pengeluaran bulan ini")
