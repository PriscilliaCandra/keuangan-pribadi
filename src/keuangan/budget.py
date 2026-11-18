from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .db import Database
from .formatting import bar, rupiah

TOTAL = "*"  # kunci anggaran total bulanan
WARN_AT = 80
FULL_AT = 100


def month_bounds(d: date) -> tuple[date, date]:
    """[awal bulan, awal bulan berikutnya)."""
    first = d.replace(day=1)
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first, nxt


def label(category: str) -> str:
    return "total bulanan" if category == TOTAL else category


@dataclass
class BudgetStatus:
    category: str
    limit: int
    spent: int

    @property
    def pct(self) -> int:
        return int(self.spent * 100 / self.limit)


def statuses(db: Database, chat_id: int, today: date) -> list[BudgetStatus]:
    start, end = month_bounds(today)
    result = []
    for category, limit in sorted(db.get_budgets(chat_id).items(), key=lambda kv: kv[0] != TOTAL):
        spent = db.sum_between(chat_id, start, end, None if category == TOTAL else category)
        result.append(BudgetStatus(category, limit, spent))
    return result


def format_statuses(items: list[BudgetStatus]) -> str:
    if not items:
        return "Belum ada anggaran. Atur dengan: /anggaran makanan 1,5jt"
    lines = ["💰 Anggaran bulan ini"]
    for s in items:
        icon = "🚨" if s.pct >= FULL_AT else "⚠️" if s.pct >= WARN_AT else "✅"
        lines.append(
            f"{icon} {label(s.category)}: {rupiah(s.spent)} / {rupiah(s.limit)} "
            f"({s.pct}%) {bar(s.pct)}"
        )
    return "\n".join(lines)


def check_alerts(db: Database, chat_id: int, category: str, today: date) -> list[str]:
    """Peringatan baru (80% / 100%) setelah pengeluaran di `category`. Tiap level sekali per bulan."""
    month_key = today.strftime("%Y-%m")
    budgets = db.get_budgets(chat_id)
    start, end = month_bounds(today)
    messages: list[str] = []
    for key in (category, TOTAL):
        limit = budgets.get(key)
        if not limit:
            continue
        spent = db.sum_between(chat_id, start, end, None if key == TOTAL else key)
        pct = int(spent * 100 / limit)
        level = FULL_AT if pct >= FULL_AT else WARN_AT if pct >= WARN_AT else 0
        if not level or not db.try_mark_alert(chat_id, key, month_key, level):
            continue
        if level == FULL_AT:
            over = spent - limit
            tail = f"melebihi {rupiah(over)}" if over > 0 else "pas habis"
            messages.append(
                f"🚨 Anggaran {label(key)} sudah habis: {rupiah(spent)} dari {rupiah(limit)} ({tail})."
            )
        else:
            messages.append(
                f"⚠️ Anggaran {label(key)} sudah {pct}%: {rupiah(spent)} dari {rupiah(limit)}. "
                f"Sisa {rupiah(limit - spent)}."
            )
    return messages
