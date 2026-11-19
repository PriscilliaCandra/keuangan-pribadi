from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta

from .db import Database
from .formatting import bar, rupiah, tanggal


def summary(db: Database, chat_id: int, start: date, end_exclusive: date, title: str, top: int = 3) -> str:
    """Ringkasan pengeluaran per kategori untuk [start, end_exclusive), dibanding periode sebelumnya."""
    items = db.expenses_between(chat_id, start, end_exclusive)
    last_day = end_exclusive - timedelta(days=1)
    header = f"📊 {title} ({tanggal(start)} – {tanggal(last_day)})"
    if not items:
        return f"{header}\nBelum ada pengeluaran tercatat."

    total = sum(e.amount for e in items)
    by_cat: dict[str, int] = defaultdict(int)
    for e in items:
        by_cat[e.category] += e.amount

    lines = [header, f"Total: {rupiah(total)} dari {len(items)} transaksi"]

    span = end_exclusive - start
    prev_total = db.sum_between(chat_id, start - span, start)
    if prev_total > 0:
        delta = total - prev_total
        pct = abs(delta) * 100 // prev_total
        arrow = "naik" if delta > 0 else "turun"
        if delta == 0:
            lines.append("Sama dengan periode sebelumnya.")
        else:
            lines.append(f"{'📈' if delta > 0 else '📉'} {arrow} {pct}% dari periode sebelumnya "
                         f"({rupiah(prev_total)})")

    lines.append("")
    for cat, amount in sorted(by_cat.items(), key=lambda kv: -kv[1]):
        share = amount * 100 / total
        lines.append(f"{bar(share)} {cat}: {rupiah(amount)} ({share:.0f}%)")

    biggest = sorted(items, key=lambda e: -e.amount)[:top]
    lines.append("")
    lines.append("Terbesar:")
    for e in biggest:
        lines.append(f"• {rupiah(e.amount)} — {e.description} ({tanggal(e.spent_on)})")
    return "\n".join(lines)


# ---------- permintaan "jelaskan/rincian pengeluaranku ..." (tanpa AI: cepat, akurat, tanpa kuota) ----------
_DETAIL_VERB = re.compile(
    r"^\s*(?:tolong\s+|coba\s+|boleh\s+)?(?:jelaskan|jelasin|ceritakan|ceritain|rincikan|rinci|rangkum|ringkas|"
    r"analisis|analisa|sebutkan|sebutin|tampilkan|tunjukkan|lihat|liat|cek|kasih\s+(?:tahu|tau)|"
    r"detail|rincian|laporan)\b", re.I)
_PERIODS = (  # (pola, judul) urut dari yang paling spesifik
    (re.compile(r"\bbulan\s+lalu\b", re.I), "bulan lalu"),
    (re.compile(r"\bbulan\s+ini\b", re.I), "bulan ini"),
    (re.compile(r"\b(?:minggu|pekan)\s+ini\b|\b7\s+hari\b", re.I), "7 hari"),
    (re.compile(r"\bhari\s+ini\b", re.I), "hari ini"),
    (re.compile(r"\btahun\s+ini\b", re.I), "tahun ini"),
    (re.compile(r"\bselama\s+ini\b|\bsejauh\s+ini\b|\bsampai\s+sekarang\b|\bkeseluruhan\b|\bsemua\b", re.I),
     "semua waktu"),
)
_SUBJECT = re.compile(r"\b(?:pengeluaran\w*|belanja\w*|keuangan\w*|uang\w*|duit\w*)\b", re.I)
_FILLER = re.compile(r"\b(?:dong|deh|ya|aja|saja|tolong|saya|aku|ku|nya|yang|secara|lengkap|rinci|detail|"
                     r"semua|selama|sejauh|ini|sampai|sekarang|keseluruhan|bulan|minggu|pekan|tahun|hari|lalu|"
                     r"dan|dari|untuk|ku|kepada)\b", re.I)


def parse_detail_request(text: str, today: date) -> tuple[date, date, str] | None:
    """'jelaskan detail pengeluaranku selama ini' -> (mulai, akhir_eksklusif, judul); None bila bukan
    permintaan rincian umum (mis. menyebut kata kunci tertentu: itu urusan agen AI)."""
    if not _DETAIL_VERB.match(text) or not _SUBJECT.search(text):
        return None
    rest = _DETAIL_VERB.sub(" ", text, count=1)
    title = "bulan ini"
    for pattern, name in _PERIODS:
        if pattern.search(rest):
            title, rest = name, pattern.sub(" ", rest)
            break
    leftover = re.sub(r"[^\w]+", " ", _FILLER.sub(" ", _SUBJECT.sub(" ", rest))).strip()
    if leftover:
        return None
    month_start = today.replace(day=1)
    tomorrow = today + timedelta(days=1)
    if title == "bulan lalu":
        prev = (month_start - timedelta(days=1)).replace(day=1)
        return prev, month_start, title
    ranges = {
        "bulan ini": (month_start, tomorrow), "7 hari": (today - timedelta(days=6), tomorrow),
        "hari ini": (today, tomorrow), "tahun ini": (today.replace(month=1, day=1), tomorrow),
        "semua waktu": (date(2000, 1, 1), tomorrow),
    }
    start, end = ranges[title]
    return start, end, title
