from datetime import date

import pytest

from keuangan.parser import parse_quick_delete, parse_quick_edit
from keuangan.service import Service
from tests.conftest import CHAT, TODAY


@pytest.fixture
def svc(db):
    return Service(db, None, clock=lambda: TODAY)


# ---------- parser: koreksi ----------
@pytest.mark.parametrize("text,in_reply,expected", [
    # sebagai balasan: nilai murni cukup
    ("transportasi", True, [("kategori", "Transportasi")]),
    ("40rb", True, [("nominal", "40rb")]),
    ("kemarin", True, [("tanggal", "kemarin")]),
    ("40rb kemarin transport", True, [("tanggal", "kemarin"), ("nominal", "40rb"), ("kategori", "Transportasi")]),
    ("kategori belanja", True, [("kategori", "belanja")]),
    ("ganti kategori jadi kesehatan", True, [("kategori", "kesehatan")]),
    # tanpa balasan: harus eksplisit
    ("ubah jadi 40rb", False, [("nominal", "40rb")]),
    ("ganti jadi transportasi", False, [("kategori", "Transportasi")]),
    ("kategorinya transport", False, [("kategori", "transport")]),
    ("harusnya 40rb", False, [("nominal", "40rb")]),
    ("harusnya kemarin", False, [("tanggal", "kemarin")]),
    ("ubah yang tadi kategori belanja", False, [("kategori", "belanja")]),
    ("koreksi nominal 1,5jt dan tanggal kemarin", False,
     [("nominal", "1,5jt"), ("tanggal", "kemarin")]),
    ("nominalnya 40rb", False, [("nominal", "40rb")]),
    ("edit catatan makan malam", False, [("catatan", "makan malam")]),
])
def test_quick_edit_recognized(text, in_reply, expected):
    assert sorted(parse_quick_edit(text, in_reply)) == sorted(expected)


@pytest.mark.parametrize("text,in_reply", [
    ("ganti oli 150rb", False),          # pengeluaran, bukan koreksi
    ("ganti ban motor 300rb", False),
    ("ubah oli 100rb", False),
    ("transportasi", False),             # tanpa balasan & tanpa kata kerja
    ("40rb", False),
    ("kemarin", False),
    ("kopi 25rb", True),                 # balasan, tapi ada kata tak dikenal -> catatan baru
    ("makan siang 35rb", True),
    ("halo apa kabar", True),
    ("ubah", False), ("ganti", False), ("kategorinya", False), ("harusnya", False),
    ("kategori apa paling boros", False),
])
def test_quick_edit_not_triggered(text, in_reply):
    assert parse_quick_edit(text, in_reply) is None


@pytest.mark.parametrize("text,in_reply,expected", [
    ("hapus yang tadi", False, True), ("hapus yang terakhir", False, True),
    ("hapus itu", False, True), ("tolong hapus yang barusan ya", False, True),
    ("hapus", True, True), ("hapus dong", True, True),
    ("hapus", False, False), ("hapus semua", False, False), ("hapus semua", True, False),
    ("hapus kopi 25rb", False, False), ("kopi 25rb", True, False),
])
def test_quick_delete(text, in_reply, expected):
    assert parse_quick_delete(text, in_reply) is expected


# ---------- layanan ----------
def reply_to_for(svc_reply: str) -> str:
    return svc_reply  # teks pesan bot yang dibalas user


def test_reply_changes_the_replied_entry_not_the_latest(svc, db):
    first = svc.handle_text(CHAT, "kopi 25rb")           # ID 1
    svc.handle_text(CHAT, "bensin 50rb")                  # ID 2 (terbaru)
    out = svc.handle_text(CHAT, "belanja", reply_to=first)
    assert "Diubah" in out and "(ID 1)" in out
    assert db.get_expense(CHAT, 1).category == "Belanja"
    assert db.get_expense(CHAT, 2).category == "Transportasi"


def test_reply_with_several_values_at_once(svc, db):
    msg = svc.handle_text(CHAT, "xyz 10rb")
    out = svc.handle_text(CHAT, "40rb kemarin transport", reply_to=msg)
    e = db.get_expense(CHAT, 1)
    assert (e.amount, e.category, e.spent_on) == (40_000, "Transportasi", date(2026, 11, 16))
    assert "nominal → Rp40.000" in out and "kategori → Transportasi" in out


def test_without_reply_edits_latest_entry_and_says_so(svc, db):
    svc.handle_text(CHAT, "kopi 25rb")
    svc.handle_text(CHAT, "bensin 50rb")
    out = svc.handle_text(CHAT, "ubah jadi 40rb")
    assert db.get_expense(CHAT, 2).amount == 40_000 and db.get_expense(CHAT, 1).amount == 25_000
    assert "catatan terakhir" in out


def test_chained_edits_via_reply_to_edit_confirmation(svc, db):
    msg = svc.handle_text(CHAT, "xyz 10rb")
    confirm = svc.handle_text(CHAT, "transport", reply_to=msg)
    assert "(ID 1)" in confirm  # konfirmasi edit juga bisa dibalas lagi
    svc.handle_text(CHAT, "40rb", reply_to=confirm)
    assert db.get_expense(CHAT, 1).amount == 40_000


def test_ganti_oli_is_recorded_as_expense_not_edit(svc, db):
    svc.handle_text(CHAT, "kopi 25rb")
    out = svc.handle_text(CHAT, "ganti oli 150rb")
    assert "Dicatat" in out
    assert db.get_expense(CHAT, 1).amount == 25_000  # tidak tersentuh
    assert db.get_expense(CHAT, 2).amount == 150_000


def test_reply_with_new_expense_text_is_recorded_not_edit(svc, db):
    msg = svc.handle_text(CHAT, "kopi 25rb")
    out = svc.handle_text(CHAT, "makan siang 35rb", reply_to=msg)
    assert "Dicatat" in out and db.get_expense(CHAT, 1).amount == 25_000


def test_edit_income_by_reply(svc, db):
    msg = svc.handle_text(CHAT, "gaji 8jt")
    assert "(ID m1)" in msg
    svc.handle_text(CHAT, "9jt", reply_to=msg)
    assert db.get_income(CHAT, 1).amount == 9_000_000
    assert "tidak punya kategori" in svc.handle_text(CHAT, "belanja", reply_to=msg)


def test_invalid_value_changes_nothing(svc, db):
    msg = svc.handle_text(CHAT, "kopi 25rb")
    before = db.get_expense(CHAT, 1)
    out = svc.handle_text(CHAT, "kategorinya ngawur")
    assert "tidak dikenal" in out and db.get_expense(CHAT, 1) == before
    # satu nilai valid + satu tidak valid: tidak ada yang berubah
    out = svc.handle_text(CHAT, "ubah nominal 40rb kategori ngawur")
    assert "tidak dikenal" in out and db.get_expense(CHAT, 1) == before
    assert svc.handle_text(CHAT, "tanggalnya besok") and db.get_expense(CHAT, 1) == before


def test_edit_with_no_entries(svc):
    assert "Belum ada catatan" in svc.handle_text(CHAT, "ubah jadi 40rb")
    assert "Belum ada catatan" in svc.handle_text(CHAT, "hapus yang tadi")


def test_delete_by_reply_and_latest(svc, db):
    first = svc.handle_text(CHAT, "kopi 25rb")
    svc.handle_text(CHAT, "bensin 50rb")
    assert "Dihapus" in svc.handle_text(CHAT, "hapus", reply_to=first)
    assert db.get_expense(CHAT, 1) is None and db.get_expense(CHAT, 2) is not None
    assert "Dihapus" in svc.handle_text(CHAT, "hapus yang tadi")
    assert db.get_expense(CHAT, 2) is None


def test_plain_hapus_without_reply_deletes_nothing(svc, db):
    svc.handle_text(CHAT, "kopi 25rb")
    svc.handle_text(CHAT, "hapus")
    svc.handle_text(CHAT, "hapus semua")
    assert db.get_expense(CHAT, 1) is not None


def test_reply_cannot_touch_another_chats_entry(svc, db):
    other = db.add_expense(2, 10_000, "rahasia", "Belanja", TODAY)
    fake = f"✅ Dicatat: Rp10.000 (ID {other})"
    out = svc.handle_text(CHAT, "transport", reply_to=fake)
    assert "tidak ditemukan" in out
    assert db.get_expense(2, other).category == "Belanja"
    assert "tidak ditemukan" in svc.handle_text(CHAT, "hapus", reply_to=fake)
    assert db.get_expense(2, other) is not None


def test_reply_to_message_without_id_is_ignored(svc, db):
    svc.handle_text(CHAT, "kopi 25rb")
    out = svc.handle_text(CHAT, "transport", reply_to="Halo! Aku pencatat keuanganmu.")
    assert "nominalnya" in out  # perlakuan normal: bukan koreksi, bukan catatan valid
    assert db.get_expense(CHAT, 1).category == "Makanan & Minuman"


# ---------- bentuk '<catatan> ternyata <nilai>' ----------
from keuangan.parser import parse_quick_edit_full  # noqa: E402


@pytest.mark.parametrize("text,pairs,hint", [
    ("gaji kemarin ternyata 7 juta", [("nominal", "7 juta")], ["gaji"]),
    ("gaji ternyata 7jt", [("nominal", "7jt")], ["gaji"]),
    ("kopi tadi maksudnya 30rb", [("nominal", "30rb")], ["kopi"]),
    ("bensin harusnya 40rb kemarin", [("nominal", "40rb"), ("tanggal", "kemarin")], ["bensin"]),
    ("makan siang ternyata kategori transport", [("kategori", "transport")], ["makan", "siang"]),
])
def test_mid_sentence_correction(text, pairs, hint):
    q = parse_quick_edit_full(text, False)
    assert sorted(q.pairs) == sorted(pairs) and q.hint == hint


@pytest.mark.parametrize("text", [
    "kopi ternyata enak 25rb",          # bukan koreksi: 'enak' bukan nilai
    "ternyata",                          # tanpa nilai
    "ternyata hujan",                    # tanpa nilai yang dikenal
    "gaji 8jt",                          # catatan biasa
])
def test_mid_sentence_not_a_correction(text):
    assert parse_quick_edit_full(text, False) is None


def test_direct_ternyata_edits_latest_without_hint():
    q = parse_quick_edit_full("ternyata 7jt", False)
    assert q.pairs == [("nominal", "7jt")] and q.hint == []


def test_the_exact_scenario_from_chat_edits_instead_of_creating(svc, db):
    svc.handle_text(CHAT, "gaji 8 juta")
    svc.handle_text(CHAT, "makan siang 35rb")
    out = svc.handle_text(CHAT, "gaji kemarin ternyata 7 juta")
    assert "Diubah" in out and "Rp7.000.000" in out and "ID m1" in out
    assert db.get_income(CHAT, 1).amount == 7_000_000
    assert db.get_income(CHAT, 2) is None  # tidak ada pemasukan baru
    assert "cocok dengan 'gaji'" in out


def test_hint_picks_latest_matching_entry(svc, db):
    svc.handle_text(CHAT, "kopi 25rb")
    svc.handle_text(CHAT, "bensin 50rb")
    svc.handle_text(CHAT, "kopi susu 30rb")
    svc.handle_text(CHAT, "kopi ternyata 28rb")
    assert db.get_expense(CHAT, 3).amount == 28_000 and db.get_expense(CHAT, 1).amount == 25_000


def test_hint_without_match_changes_nothing_and_does_not_record(svc, db):
    svc.handle_text(CHAT, "kopi 25rb")
    out = svc.handle_text(CHAT, "bensin ternyata 40rb")
    assert "tidak menemukan catatan" in out and "bensin" in out
    assert db.get_expense(CHAT, 2) is None and db.get_expense(CHAT, 1).amount == 25_000


def test_hint_search_is_per_chat(svc, db):
    db.add_expense(2, 10_000, "kopi rahasia", "Makanan & Minuman", TODAY)
    out = svc.handle_text(CHAT, "kopi ternyata 99rb")
    assert "tidak menemukan" in out and db.get_expense(2, 1).amount == 10_000


def test_ordinary_sentences_with_ternyata_still_recorded(svc, db):
    out = svc.handle_text(CHAT, "kopi ternyata enak 25rb")
    assert "Dicatat" in out and db.get_expense(CHAT, 1).amount == 25_000
