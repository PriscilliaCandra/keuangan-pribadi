"""Ekspor SEMUA data satu pengguna ke ZIP berisi beberapa CSV (hak mengunduh data sendiri)."""
from __future__ import annotations

import csv
import io
import zipfile
from datetime import date, timedelta

from . import ledger
from .db import Database
from .ledger import _csv_safe

# (nama file, kolom, SQL). Semua query wajib memfilter chat_id (satu parameter).
_TABLES: list[tuple[str, list[str], str]] = [
    ("anggaran.csv", ["kategori", "batas_bulanan"],
     "SELECT category, monthly_limit FROM budgets WHERE chat_id=? ORDER BY category"),
    ("tagihan_langganan.csv",
     ["nama", "jenis", "jumlah", "tgl_jatuh_tempo", "bulan_jatuh_tempo", "siklus", "aktif", "dibuat"],
     "SELECT name, kind, amount, due_day, due_month, cycle, active, created_on FROM bills"
     " WHERE chat_id=? ORDER BY id"),
    ("target_tabungan.csv", ["nama", "target", "tenggat", "dibuat"],
     "SELECT name, target, deadline, created_on FROM goals WHERE chat_id=? ORDER BY id"),
    ("setoran_tabungan.csv", ["target", "tanggal", "jumlah"],
     "SELECT g.name, d.on_date, d.amount FROM goal_deposits d JOIN goals g ON g.id=d.goal_id"
     " WHERE d.chat_id=? AND g.chat_id=d.chat_id ORDER BY d.on_date, d.id"),
    ("utang_piutang.csv", ["orang", "arah", "jumlah", "catatan", "dibuat", "jatuh_tempo"],
     "SELECT person, direction, amount, note, created_on, due_on FROM debts WHERE chat_id=? ORDER BY id"),
    ("pembayaran_utang.csv", ["orang", "arah", "tanggal", "jumlah"],
     "SELECT d.person, d.direction, p.on_date, p.amount FROM debt_payments p JOIN debts d ON d.id=p.debt_id"
     " WHERE p.chat_id=? AND d.chat_id=p.chat_id ORDER BY p.on_date, p.id"),
    ("catatan_rutin.csv", ["nama", "jenis", "jumlah", "kategori", "tanggal", "aktif", "dibuat"],
     "SELECT name, kind, amount, category, day, active, created_on FROM recurring WHERE chat_id=? ORDER BY id"),
    ("pengaturan.csv", ["pengaturan", "nilai"],
     "SELECT 'tanggal_gajian', payday FROM user_settings WHERE chat_id=? UNION ALL"
     " SELECT 'pengingat_malam', evening_reminder FROM user_settings WHERE chat_id=?"),
]

README = """Ekspor data Keuangan Pribadi
============================
Berisi semua data milikmu. Buka dengan Excel / Google Sheets (UTF-8).

transaksi.csv          semua pengeluaran & pemasukan
anggaran.csv           batas anggaran per kategori
tagihan_langganan.csv  tagihan & langganan
target_tabungan.csv    target tabungan (+ setoran_tabungan.csv)
utang_piutang.csv      utang-piutang (+ pembayaran_utang.csv)
catatan_rutin.csv      catatan otomatis tiap bulan
pengaturan.csv         tanggal gajian & pengingat malam

Jumlah dalam rupiah. Simpan file ini di tempat yang aman: isinya data keuanganmu.
"""


def _csv(header: list[str], rows) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([_csv_safe(v) if isinstance(v, str) else ("" if v is None else v) for v in row])
    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")


def export_zip(db: Database, chat_id: int, today: date) -> bytes:
    """ZIP in-memory berisi seluruh data `chat_id` (dan hanya miliknya)."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("transaksi.csv",
                    ledger.export_csv(db, chat_id, date(2000, 1, 1), today + timedelta(days=1)))
        for name, header, sql in _TABLES:
            params = (chat_id,) * sql.count("?")
            zf.writestr(name, _csv(header, (tuple(r) for r in db.query(sql, params))))
        zf.writestr("BACA-SAYA.txt", README)
    return out.getvalue()
