"""Logika tagihan & langganan: jatuh tempo, pengingat bertahap, langganan 'lupa dibatalkan'."""
from __future__ import annotations

import calendar
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from .db import Bill, Database
from .formatting import rupiah, tanggal
from .parser import find_amount

STALE_AFTER_DAYS = 90


# ---------- jatuh tempo ----------
def _safe_date(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def next_due(bill: Bill, today: date) -> date:
    if bill.cycle == "yearly" and bill.due_month:
        c = _safe_date(today.year, bill.due_month, bill.due_day)
        return c if c >= today else _safe_date(today.year + 1, bill.due_month, bill.due_day)
    c = _safe_date(today.year, today.month, bill.due_day)
    if c >= today:
        return c
    year = today.year + (1 if today.month == 12 else 0)
    return _safe_date(year, today.month % 12 + 1, bill.due_day)


def monthly_cost(bill: Bill) -> int:
    if not bill.amount:
        return 0
    return bill.amount // 12 if bill.cycle == "yearly" else bill.amount


@dataclass
class Reminder:
    bill: Bill
    due: date
    days_left: int
    token: str  # disimpan agar pengingat yang sama tidak terkirim dua kali


def due_reminders(db: Database, chat_id: int, today: date, days_ahead: int = 3) -> list[Reminder]:
    """Pengingat dua tahap per jatuh tempo: H-`days_ahead` (awal) dan hari-H."""
    out = []
    for bill in db.list_bills(chat_id):
        due = next_due(bill, today)
        left = (due - today).days
        if left > days_ahead:
            continue
        token = f"{due.isoformat()}:{'hari-h' if left == 0 else 'awal'}"
        if bill.last_reminded == token:
            continue
        out.append(Reminder(bill, due, left, token))
    return sorted(out, key=lambda r: r.due)


def format_reminders(items: list[Reminder]) -> str:
    lines = ["📅 Pengingat jatuh tempo"]
    for r in items:
        when = "HARI INI" if r.days_left == 0 else f"{r.days_left} hari lagi ({tanggal(r.due)})"
        amount = f" — {rupiah(r.bill.amount)}" if r.bill.amount else ""
        lines.append(f"• {r.bill.name}{amount} — {when}")
    return "\n".join(lines)


# ---------- langganan yang mungkin terlupa ----------
def stale_subscriptions(db: Database, chat_id: int, today: date) -> list[Bill]:
    out = []
    for bill in db.list_bills(chat_id):
        if bill.kind != "langganan":
            continue
        baseline = bill.last_confirmed or bill.created_on
        if (today - baseline).days >= STALE_AFTER_DAYS:
            out.append(bill)
    return out


def format_stale(items: list[Bill]) -> str:
    total = sum(monthly_cost(b) for b in items)
    lines = ["🔎 Langganan ini sudah lama tidak kamu konfirmasi. Masih dipakai?"]
    for b in items:
        amount = f" ({rupiah(b.amount)}/{'tahun' if b.cycle == 'yearly' else 'bulan'})" if b.amount else ""
        lines.append(f"• {b.name}{amount}")
    if total:
        lines.append(f"Total ≈ {rupiah(total)}/bulan.")
    lines.append("Balas /pakai <nama> kalau masih dipakai, atau /batal <nama> kalau sudah berhenti.")
    return "\n".join(lines)


def format_bill_list(bills: list[Bill], today: date) -> str:
    if not bills:
        return "Belum ada tagihan/langganan. Tambah: /tagihan listrik 350rb tgl 20 atau /langganan netflix 54rb tgl 5"
    lines = ["🧾 Tagihan & langganan aktif"]
    for b in sorted(bills, key=lambda x: next_due(x, today)):
        due = next_due(b, today)
        icon = "🔁" if b.kind == "langganan" else "💡"
        amount = rupiah(b.amount) if b.amount else "jumlah belum diketahui"
        cycle = "/tahun" if b.cycle == "yearly" else "/bulan"
        lines.append(f"{icon} {b.name}: {amount}{cycle} · berikutnya {tanggal(due)}")
    total = sum(monthly_cost(b) for b in bills)
    lines.append(f"Total ≈ {rupiah(total)}/bulan")
    return "\n".join(lines)


# ---------- deteksi langganan dari pola pengeluaran ----------
_GENERIC = {"pengeluaran", "struk", ""}


def _norm(description: str) -> str:
    return re.sub(r"[^a-z]+", " ", description.lower()).strip()


def detect_recurring(db: Database, chat_id: int, today: date, min_months: int = 3) -> list[tuple[str, int]]:
    """Pengeluaran yang muncul tepat sekali tiap bulan, berturut-turut, dengan nominal sama."""
    since = date(today.year - 1, today.month, 1)
    groups: dict[tuple[str, int], list[date]] = defaultdict(list)
    for e in db.expenses_between(chat_id, since, date(today.year + 1, 1, 1)):
        key = _norm(e.description)
        if key in _GENERIC:
            continue
        groups[(key, e.amount)].append(e.spent_on)

    known = {b.name.lower() for b in db.list_bills(chat_id, active_only=False)}
    found = []
    for (key, amount), dates in groups.items():
        months = sorted({d.year * 12 + d.month for d in dates})
        if len(dates) != len(months):  # >1 kali dalam sebulan = kebiasaan, bukan langganan
            continue
        run = 1
        best = 1
        for a, b in zip(months, months[1:]):
            run = run + 1 if b == a + 1 else 1
            best = max(best, run)
        if best >= min_months and key not in known and not any(key in k for k in known):
            found.append((key.title(), amount))
    return found


def format_recurring(items: list[tuple[str, int]]) -> str:
    lines = ["🔁 Pengeluaran ini berulang tiap bulan dengan nominal sama. Mau dicatat sebagai langganan?"]
    for name, amount in items:
        lines.append(f"• {name} — {rupiah(amount)}  → /langganan {name.lower()} {amount} tgl <tanggal>")
    return "\n".join(lines)


# ---------- parsing perintah /tagihan & /langganan ----------
@dataclass
class BillInput:
    name: str
    amount: int | None
    due_day: int
    cycle: str
    due_month: int | None


def parse_bill_args(text: str) -> BillInput | None:
    """'netflix 54rb tgl 5' | 'listrik 350rb 20' | 'pajak motor 2jt tgl 10 bulan 3 tahunan'."""
    t = f" {text.strip()} "
    cycle, due_month, due_day = "monthly", None, None

    if re.search(r"\b(tahunan|yearly|setahun)\b", t, re.I):
        cycle = "yearly"
        t = re.sub(r"\b(tahunan|yearly|setahun)\b", " ", t, flags=re.I)
    m = re.search(r"\b(?:bln|bulan)\s*(\d{1,2})\b", t, re.I)
    if m:
        due_month, cycle = int(m.group(1)), "yearly"
        t = t[: m.start()] + " " + t[m.end():]
    m = re.search(r"\b(?:tgl|tanggal)\s*(\d{1,2})\b", t, re.I)
    if m:
        due_day = int(m.group(1))
        t = t[: m.start()] + " " + t[m.end():]

    amount = None
    found = find_amount(t)
    if found:
        amount, (s, e) = found
        t = t[:s] + " " + t[e:]
    if due_day is None:
        m = re.search(r"\s(\d{1,2})\s*$", t)
        if m:
            due_day = int(m.group(1))
            t = t[: m.start()]

    name = re.sub(r"\s+", " ", t).strip(" -:,.")
    if not name or due_day is None or not 1 <= due_day <= 31:
        return None
    if due_month is not None and not 1 <= due_month <= 12:
        return None
    if cycle == "yearly" and due_month is None:
        return None
    return BillInput(name[:60], amount, due_day, cycle, due_month)
