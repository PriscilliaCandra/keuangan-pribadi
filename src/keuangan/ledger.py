"""Buku kas: arus kas, riwayat, ubah/hapus per ID, ekspor CSV. Bebas dari Telegram."""
from __future__ import annotations

import csv
import io
import re
from datetime import date

from .categories import CATEGORIES, match_category
from .db import Database, Expense, Income
from .formatting import rupiah, tanggal
from .parser import find_amount, parse_date_text

INCOME_LABEL = "Pemasukan"


# ---------- arus kas ----------
def cashflow(db: Database, chat_id: int, start: date, end_exclusive: date, title: str) -> str:
    income = db.sum_income_between(chat_id, start, end_exclusive)
    spent = db.sum_between(chat_id, start, end_exclusive)
    net = income - spent
    lines = [
        f"💼 {title}",
        f"Pemasukan:   {rupiah(income)}",
        f"Pengeluaran: {rupiah(spent)}",
        f"{'🟢' if net >= 0 else '🔴'} Selisih: {rupiah(net)}",
    ]
    if income > 0:
        rate = net * 100 // income
        lines.append(f"Ditabung: {rate}% dari pemasukan" if net >= 0
                     else f"Defisit: {-rate}% dari pemasukan")
    elif spent > 0:
        lines.append("Belum ada pemasukan tercatat. Catat dengan: gaji 8jt")
    return "\n".join(lines)


# ---------- riwayat ----------
def ref_token(kind: str, entry: Expense | Income) -> str:
    """ID tampilan: pengeluaran '12', pemasukan 'm3'."""
    return f"m{entry.id}" if kind == "masuk" else str(entry.id)


def format_entry(kind: str, entry: Expense | Income) -> str:
    if kind == "masuk":
        return (f"{ref_token(kind, entry):>4} · {tanggal(entry.received_on)} · 💵 +{rupiah(entry.amount)}"
                f" · {entry.description}")
    return (f"{ref_token(kind, entry):>4} · {tanggal(entry.spent_on)} · {rupiah(entry.amount)}"
            f" · {entry.description} ({entry.category})")


def history(db: Database, chat_id: int, limit: int = 10) -> str:
    entries = db.recent_entries(chat_id, limit)
    if not entries:
        return "Belum ada catatan."
    lines = ["🕘 Catatan terakhir (ID di kiri; pemasukan berawalan m)"]
    lines += [format_entry(k, e) for k, e in entries]
    lines.append("Ubah: /edit <id> kategori transport · Hapus: /hapus <id>")
    return "\n".join(lines)


def parse_ref(token: str) -> tuple[str, int] | None:
    m = re.fullmatch(r"#?(m)?(\d{1,9})", token.strip().lower())
    if not m:
        return None
    return ("masuk" if m.group(1) else "keluar"), int(m.group(2))


def delete_entry(db: Database, chat_id: int, args: str) -> str:
    """Tanpa argumen: hapus catatan terakhir (jenis apa pun). Dengan ID: hapus yang itu."""
    token = args.strip()
    if not token:
        gone = db.delete_latest_entry(chat_id)
        if gone is None:
            return "Belum ada catatan untuk dihapus."
        kind, entry = gone
        return "🗑️ Dihapus: " + format_entry(kind, entry).strip()
    ref = parse_ref(token)
    if ref is None:
        return "Format: /hapus (catatan terakhir) atau /hapus <id>, lihat ID di /riwayat."
    kind, row_id = ref
    entry = (db.delete_expense if kind == "keluar" else db.delete_income)(chat_id, row_id)
    if entry is None:
        return f"ID {token} tidak ditemukan. Lihat /riwayat."
    return "🗑️ Dihapus: " + format_entry(kind, entry).strip()


# ---------- ubah ----------
_FIELDS = {
    "nominal": "amount", "jumlah": "amount", "harga": "amount",
    "kategori": "category",
    "catatan": "description", "deskripsi": "description", "nama": "description",
    "tanggal": "date", "tgl": "date",
}
EDIT_HELP = (
    "Format: /edit <id> <bagian> <nilai>\n"
    "• /edit 12 kategori transport\n• /edit 12 nominal 40rb\n"
    "• /edit 12 tanggal kemarin   (atau 2026-11-05, 5/11)\n• /edit 12 catatan makan malam\n"
    "ID ada di /riwayat. Pemasukan berawalan m (mis. m3)."
)


def edit_entry(db: Database, chat_id: int, args: str, today: date) -> str:
    """Perintah /edit <id> <bagian> <nilai>."""
    parts = args.strip().split(None, 2)
    if len(parts) < 3:
        return EDIT_HELP
    return apply_edits(db, chat_id, parts[0], [(parts[1], parts[2])], today)


def apply_edits(db: Database, chat_id: int, ref_text: str, pairs: list[tuple[str, str]],
                today: date) -> str:
    """Terapkan satu atau beberapa perubahan (bagian, nilai) pada satu catatan.

    Semua perubahan divalidasi dulu; kalau ada satu yang tidak valid, tidak ada yang diubah.
    """
    ref = parse_ref(ref_text)
    if ref is None or not pairs:
        return EDIT_HELP
    kind, row_id = ref
    changes: dict = {}
    shown: list[str] = []
    for field_word, raw in pairs:
        field = _FIELDS.get(field_word.lower())
        value = raw.strip()
        if field is None or not value:
            return EDIT_HELP
        if field == "category":
            if kind == "masuk":
                return "Pemasukan tidak punya kategori. Ubah nominal, tanggal, atau catatan."
            category = match_category(value)
            if category is None:
                return "Kategori tidak dikenal. Pilihan: " + ", ".join(CATEGORIES)
            changes["category"] = category
            shown.append(f"kategori → {category}")
        elif field == "amount":
            found = find_amount(value)
            if not found:
                return "Nominal tidak terbaca. Contoh: 40rb atau 1,5jt"
            changes["amount"] = found[0]
            shown.append(f"nominal → {rupiah(found[0])}")
        elif field == "date":
            when = parse_date_text(value, today)
            if when is None:
                return "Tanggal tidak terbaca atau di masa depan. Contoh: kemarin, 2026-11-05, 5/11"
            changes["received_on" if kind == "masuk" else "spent_on"] = when
            shown.append(f"tanggal → {tanggal(when)}")
        else:
            changes["description"] = value[:120]
            shown.append(f"catatan → {value[:120]}")

    update = db.update_income if kind == "masuk" else db.update_expense
    if not update(chat_id, row_id, **changes):
        return f"ID {ref_text} tidak ditemukan. Lihat /riwayat."
    entry = (db.get_income if kind == "masuk" else db.get_expense)(chat_id, row_id)
    return (f"✏️ Diubah ({', '.join(shown)}) (ID {ref_token(kind, entry)}):\n"
            + format_entry(kind, entry).strip())


# ---------- ekspor ----------
def _csv_safe(value: str) -> str:
    """Cegah formula injection di Excel/Sheets: sel yang diawali = + - @ dianggap teks."""
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def export_csv(db: Database, chat_id: int, start: date, end_exclusive: date) -> bytes:
    """CSV UTF-8 (dengan BOM agar Excel membaca huruf dengan benar), urut tanggal."""
    rows: list[tuple[date, int, list]] = []
    for e in db.expenses_between(chat_id, start, end_exclusive):
        rows.append((e.spent_on, e.id, ["keluar", e.id, e.spent_on.isoformat(), e.amount,
                                        e.category, _csv_safe(e.description), e.source]))
    for i in db.incomes_between(chat_id, start, end_exclusive):
        rows.append((i.received_on, i.id, ["masuk", f"m{i.id}", i.received_on.isoformat(),
                                           i.amount, INCOME_LABEL, _csv_safe(i.description),
                                           i.source]))
    rows.sort(key=lambda r: (r[0], r[2][0], r[1]))
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(["jenis", "id", "tanggal", "jumlah", "kategori", "catatan", "sumber"])
    writer.writerows(r[2] for r in rows)
    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")
