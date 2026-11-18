"""Insight otomatis dari kebiasaan pengeluaran. Murni aturan + angka (tanpa AI): hasilnya selalu
bisa ditelusuri ke datanya, tidak mengarang. Hanya membaca data satu pengguna."""
from __future__ import annotations

import calendar
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from .db import Database
from .formatting import rupiah

MAX_INSIGHTS = 4
MIN_EXPENSES = 5


@dataclass(frozen=True)
class Insight:
    kind: str       # naik | turun | kebiasaan | akhir_pekan | proyeksi | defisit
    text: str
    weight: int     # makin besar makin penting (selisih rupiah)


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _prev_month_start(d: date) -> date:
    return _month_start(_month_start(d) - timedelta(days=1))


def _by_category(expenses) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for e in expenses:
        out[e.category] += e.amount
    return out


def _pct(a: int, b: int) -> int:
    return round((a - b) * 100 / b)


def _category_changes(cur: dict[str, int], prev: dict[str, int]) -> list[Insight]:
    found = []
    for cat in set(cur) | set(prev):
        c, p = cur.get(cat, 0), prev.get(cat, 0)
        if p > 0 and c >= p * 1.3 and c - p >= 50_000:
            found.append(Insight("naik", f"📈 {cat} naik {_pct(c, p)}% dibanding periode yang sama bulan lalu "
                                         f"({rupiah(p)} → {rupiah(c)}).", c - p))
        elif p == 0 and c >= 100_000:
            found.append(Insight("naik", f"🆕 {cat} baru muncul bulan ini: {rupiah(c)}.", c))
        elif p > 0 and c <= p * 0.7 and p - c >= 100_000:
            found.append(Insight("turun", f"👏 {cat} turun {abs(_pct(c, p))}% dibanding periode yang sama "
                                          f"bulan lalu ({rupiah(p)} → {rupiah(c)}). Pertahankan!", (p - c) // 2))
    return found


def _habits(expenses) -> list[Insight]:
    """Pengeluaran kecil yang berulang (kopi, parkir, jajan): jumlahnya sering tak terasa."""
    groups: dict[str, list[int]] = defaultdict(list)
    for e in expenses:
        if e.source == "rutin":
            continue
        words = re.findall(r"[a-zA-Z]{3,}", e.description.lower())
        if words:
            groups[words[0]].append(e.amount)
    found = []
    for word, amounts in groups.items():
        total = sum(amounts)
        if len(amounts) >= 5 and total / len(amounts) <= 60_000:
            found.append(Insight("kebiasaan", f"☕ \"{word}\" muncul {len(amounts)}x bulan ini, total {rupiah(total)}. "
                                              f"Kalau terus begini ≈ {rupiah(total * 12)} setahun.", total // 2))
    return found


def _weekend(expenses, today: date) -> list[Insight]:
    start = today - timedelta(days=29)
    recent = [e for e in expenses if e.spent_on >= start]
    if len(recent) < 10:
        return []
    days = [start + timedelta(days=i) for i in range(30)]
    wk = [d for d in days if d.weekday() >= 5]
    wd = [d for d in days if d.weekday() < 5]
    wk_total = sum(e.amount for e in recent if e.spent_on.weekday() >= 5)
    wd_total = sum(e.amount for e in recent if e.spent_on.weekday() < 5)
    wk_avg, wd_avg = wk_total / len(wk), wd_total / len(wd)
    if wd_avg > 0 and wk_avg >= wd_avg * 1.5 and wk_total >= 200_000:
        return [Insight("akhir_pekan", f"🎉 Akhir pekan lebih boros: rata-rata {rupiah(round(wk_avg))}/hari "
                                       f"vs {rupiah(round(wd_avg))}/hari di hari kerja (30 hari terakhir).",
                        round(wk_avg - wd_avg))]
    return []


def _projection(cur_total: int, prev_total: int, today: date) -> list[Insight]:
    if today.day < 7 or prev_total <= 0:
        return []
    days = calendar.monthrange(today.year, today.month)[1]
    projected = round(cur_total / today.day * days)
    if projected >= prev_total * 1.2 and projected - prev_total >= 100_000:
        return [Insight("proyeksi", f"🔮 Dengan laju sekarang, pengeluaran bulan ini ≈ {rupiah(projected)} — "
                                    f"{_pct(projected, prev_total)}% di atas total bulan lalu ({rupiah(prev_total)}).",
                        projected - prev_total)]
    return []


def build_insights(db: Database, chat_id: int, today: date) -> list[Insight]:
    """Insight terpenting (maks 4), urut dari yang paling berdampak. Kosong bila data belum cukup."""
    start = _month_start(today)
    prev_start = _prev_month_start(today)
    month_end = start.replace(day=calendar.monthrange(today.year, today.month)[1]) + timedelta(days=1)
    span_end = min(prev_start + timedelta(days=today.day), start)  # periode sama bulan lalu
    cur = db.expenses_between(chat_id, start, today + timedelta(days=1))
    prev_same = db.expenses_between(chat_id, prev_start, span_end)
    prev_all = db.sum_between(chat_id, prev_start, start)
    window = db.expenses_between(chat_id, today - timedelta(days=29), today + timedelta(days=1))
    if len(cur) + len(prev_same) < MIN_EXPENSES:
        return []
    cur_total = sum(e.amount for e in cur)
    insights = _category_changes(_by_category(cur), _by_category(prev_same)) if prev_same else []
    insights += _habits(cur) + _weekend(window, today) + _projection(cur_total, prev_all, today)
    income = db.sum_income_between(chat_id, start, month_end)
    if income > 0 and cur_total > income:
        insights.append(Insight("defisit", f"⚠️ Pengeluaran bulan ini ({rupiah(cur_total)}) sudah melebihi "
                                           f"pemasukan ({rupiah(income)}).", cur_total - income))
    insights.sort(key=lambda i: -i.weight)
    return insights[:MAX_INSIGHTS]


def format_insights(items: list[Insight]) -> str:
    if not items:
        return ("Belum ada insight yang menonjol, atau datamu belum cukup (butuh ±5 pengeluaran). "
                "Terus catat, ya.")
    return "💡 Insight keuanganmu\n\n" + "\n\n".join(i.text for i in items)
