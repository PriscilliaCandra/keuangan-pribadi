from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from .db import Database
from .formatting import bar, rupiah, tanggal


def summary(db: Database, chat_id: int, start: date, end_exclusive: date, title: str) -> str:
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

    biggest = sorted(items, key=lambda e: -e.amount)[:3]
    lines.append("")
    lines.append("Terbesar:")
    for e in biggest:
        lines.append(f"• {rupiah(e.amount)} — {e.description} ({tanggal(e.spent_on)})")
    return "\n".join(lines)
