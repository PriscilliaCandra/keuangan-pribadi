"""Perencanaan: tanggal gajian, uang aman per hari, prediksi anggaran, dan target tabungan."""
from __future__ import annotations

import calendar
import math
import re
from dataclasses import dataclass
from datetime import date, timedelta

from . import bills as bills_mod
from . import budget
from .db import Database
from .formatting import bar, rupiah, tanggal


def parse_deadline(text: str, today: date) -> date | None:
    """Tenggat target: 2027-06-30, 2027-06 (akhir bulan), atau 06/2027. Harus setelah hari ini."""
    t = text.strip()
    result: date | None = None
    try:
        if m := re.fullmatch(r"(\d{4})-(\d{1,2})(?:-(\d{1,2}))?", t):
            y, mo = int(m.group(1)), int(m.group(2))
            result = date(y, mo, int(m.group(3))) if m.group(3) else _safe(y, mo, 31)
        elif m := re.fullmatch(r"(\d{1,2})/(\d{4})", t):
            result = _safe(int(m.group(2)), int(m.group(1)), 31)
    except ValueError:
        return None
    return result if result and result > today else None


def _safe(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def _shift_month(d: date, delta: int) -> tuple[int, int]:
    index = d.year * 12 + d.month - 1 + delta
    return index // 12, index % 12 + 1


# ---------- siklus gajian ----------
def cycle(today: date, payday: int) -> tuple[date, date]:
    """(awal siklus, gajian berikutnya): awal = tanggal gajian terakhir (hari ini bila hari gajian)."""
    this_month = _safe(today.year, today.month, payday)
    if this_month <= today:
        y, m = _shift_month(today, 1)
        return this_month, _safe(y, m, payday)
    y, m = _shift_month(today, -1)
    return _safe(y, m, payday), this_month


# ---------- penyimpanan ----------
@dataclass
class Goal:
    id: int
    chat_id: int
    name: str
    target: int
    deadline: date | None
    created_on: date
    saved: int = 0

    @property
    def pct(self) -> int:
        return max(0, min(100, self.saved * 100 // self.target))

    @property
    def remaining(self) -> int:
        return max(0, self.target - self.saved)


class PlanStore:
    def __init__(self, db: Database):
        self.db = db

    # --- tanggal gajian ---
    def get_payday(self, chat_id: int) -> int | None:
        rows = self.db.query("SELECT payday FROM user_settings WHERE chat_id=?", (chat_id,))
        return rows[0]["payday"] if rows else None

    def set_payday(self, chat_id: int, payday: int) -> None:
        if not 1 <= payday <= 31:
            raise ValueError("tanggal gajian harus 1-31")
        self.db.execute(
            "INSERT INTO user_settings (chat_id, payday) VALUES (?, ?)"
            " ON CONFLICT(chat_id) DO UPDATE SET payday=excluded.payday", (chat_id, payday))

    # --- pengingat malam (opt-in) ---
    def get_evening(self, chat_id: int) -> bool:
        rows = self.db.query("SELECT evening_reminder FROM user_settings WHERE chat_id=?", (chat_id,))
        return bool(rows and rows[0]["evening_reminder"])

    def set_evening(self, chat_id: int, on: bool) -> None:
        self.db.execute(
            "INSERT INTO user_settings (chat_id, evening_reminder) VALUES (?, ?)"
            " ON CONFLICT(chat_id) DO UPDATE SET evening_reminder=excluded.evening_reminder",
            (chat_id, int(on)))

    def evening_chat_ids(self) -> set[int]:
        return {r["chat_id"] for r in self.db.query(
            "SELECT chat_id FROM user_settings WHERE evening_reminder=1")}

    # --- target tabungan ---
    _SELECT = ("SELECT g.*, COALESCE((SELECT SUM(amount) FROM goal_deposits d WHERE d.goal_id=g.id), 0)"
               " AS saved FROM goals g")

    @staticmethod
    def _goal(row) -> Goal:
        return Goal(row["id"], row["chat_id"], row["name"], row["target"],
                    date.fromisoformat(row["deadline"]) if row["deadline"] else None,
                    date.fromisoformat(row["created_on"]), row["saved"])

    def create_goal(self, chat_id: int, name: str, target: int, deadline: date | None,
                    today: date) -> tuple[Goal, bool]:
        """Buat target baru, atau perbarui target/tenggatnya bila namanya sudah ada."""
        key = name.strip().lower()
        existing = self.db.query("SELECT id FROM goals WHERE chat_id=? AND name_key=?", (chat_id, key))
        iso = deadline.isoformat() if deadline else None
        if existing:
            self.db.execute("UPDATE goals SET target=?, deadline=? WHERE id=? AND chat_id=?",
                            (target, iso, existing[0]["id"], chat_id))
            return self.get_goal(chat_id, existing[0]["id"]), False
        cur = self.db.execute(
            "INSERT INTO goals (chat_id, name, name_key, target, deadline, created_on) VALUES (?,?,?,?,?,?)",
            (chat_id, name.strip(), key, target, iso, today.isoformat()))
        return self.get_goal(chat_id, int(cur.lastrowid)), True

    def get_goal(self, chat_id: int, goal_id: int) -> Goal:
        rows = self.db.query(self._SELECT + " WHERE g.chat_id=? AND g.id=?", (chat_id, goal_id))
        return self._goal(rows[0])

    def list_goals(self, chat_id: int) -> list[Goal]:
        rows = self.db.query(self._SELECT + " WHERE g.chat_id=? ORDER BY g.created_on, g.id", (chat_id,))
        return [self._goal(r) for r in rows]

    def find_goal(self, chat_id: int, text: str) -> Goal | None:
        """Cocokkan nama (persis dulu, lalu mengandung). Selalu terbatas pada chat_id."""
        key = text.strip().lower()
        if not key:
            return None
        goals = self.list_goals(chat_id)
        return next((g for g in goals if g.name.lower() == key), None) or \
            next((g for g in goals if key in g.name.lower() or g.name.lower() in key), None)

    def deposit(self, chat_id: int, goal: Goal, amount: int, on_date: date) -> Goal | None:
        """Tambah (amount > 0) atau tarik (amount < 0) tabungan. None bila saldo jadi negatif."""
        if goal.chat_id != chat_id or amount == 0 or goal.saved + amount < 0:
            return None
        self.db.execute("INSERT INTO goal_deposits (goal_id, chat_id, amount, on_date) VALUES (?,?,?,?)",
                        (goal.id, chat_id, amount, on_date.isoformat()))
        return self.get_goal(chat_id, goal.id)

    def delete_goal(self, chat_id: int, goal_id: int) -> bool:
        self.db.execute("DELETE FROM goal_deposits WHERE goal_id=? AND chat_id=?", (goal_id, chat_id))
        return self.db.execute("DELETE FROM goals WHERE id=? AND chat_id=?", (goal_id, chat_id)).rowcount == 1

    def list_deposits(self, chat_id: int) -> list[tuple[str, date, int]]:
        """(nama target, tanggal, jumlah) semua setoran/penarikan milik chat_id."""
        rows = self.db.query(
            "SELECT g.name AS name, d.on_date AS on_date, d.amount AS amount FROM goal_deposits d"
            " JOIN goals g ON g.id = d.goal_id WHERE d.chat_id=? AND g.chat_id=? ORDER BY d.on_date, d.id",
            (chat_id, chat_id))
        return [(r["name"], date.fromisoformat(r["on_date"]), r["amount"]) for r in rows]

    def deposits_between(self, chat_id: int, start: date, end_exclusive: date) -> int:
        rows = self.db.query(
            "SELECT COALESCE(SUM(amount), 0) AS s FROM goal_deposits"
            " WHERE chat_id=? AND on_date >= ? AND on_date < ?",
            (chat_id, start.isoformat(), end_exclusive.isoformat()))
        return int(rows[0]["s"])


# ---------- uang aman per hari ----------
@dataclass
class SafeDaily:
    mode: str                 # "anggaran" | "pemasukan"
    payday: int
    payday_is_default: bool
    cycle_start: date
    next_payday: date
    days_left: int
    available: int            # uang yang masih boleh dipakai sampai gajian
    per_day: int
    avg7: int                 # rata-rata pengeluaran 7 hari terakhir
    depletion: date | None    # perkiraan uang habis bila laju 7 hari berlanjut (sebelum gajian)
    upcoming_bills: int = 0
    saved: int = 0

    @property
    def over_pace(self) -> bool:
        return self.avg7 > self.per_day


def safe_daily(db: Database, plans: PlanStore, chat_id: int, today: date) -> SafeDaily | None:
    """None bila belum ada dasar hitungan (belum ada anggaran total maupun pemasukan di siklus ini)."""
    stored = plans.get_payday(chat_id)
    payday = stored or 1
    start, nxt = cycle(today, payday)
    days_left = max(1, (nxt - today).days)
    tomorrow = today + timedelta(days=1)
    spent = db.sum_between(chat_id, start, tomorrow)

    total_budget = db.get_budgets(chat_id).get(budget.TOTAL)
    upcoming = saved = 0
    if total_budget:
        mode, available = "anggaran", total_budget - spent
    else:
        income = db.sum_income_between(chat_id, start, tomorrow)
        if income <= 0:
            return None
        active = db.list_bills(chat_id)
        upcoming = sum(b.amount or 0 for b in active if today <= bills_mod.next_due(b, today) < nxt)
        saved = plans.deposits_between(chat_id, start, tomorrow)
        mode, available = "pemasukan", income - spent - upcoming - saved

    per_day = max(0, available) // days_left
    avg7 = db.sum_between(chat_id, today - timedelta(days=6), tomorrow) // 7
    depletion = None
    if available > 0 and avg7 > 0:
        candidate = today + timedelta(days=int(available // avg7))
        if candidate < nxt:
            depletion = candidate
    return SafeDaily(mode, payday, stored is None, start, nxt, days_left, available, per_day,
                     avg7, depletion, upcoming, saved)


def format_safe_daily(s: SafeDaily | None) -> str:
    if s is None:
        return ("Aku belum punya dasar untuk menghitung uang aman harianmu.\n"
                "Catat pemasukan (mis. gaji 8jt) atau atur anggaran total (/anggaran total 5jt), "
                "lalu atur tanggal gajian dengan /gajian 25.")
    basis = "sisa anggaran total" if s.mode == "anggaran" else "pemasukan − pengeluaran − tagihan & tabungan"
    lines = ["🧮 Uang aman per hari"]
    if s.available <= 0:
        lines.append(f"🔴 Sudah melewati batas: kurang {rupiah(-s.available)} "
                     f"({s.days_left} hari lagi sampai {tanggal(s.next_payday)}).")
    else:
        lines.append(f"{rupiah(s.per_day)} per hari selama {s.days_left} hari, sampai {tanggal(s.next_payday)}")
        lines.append(f"Sisa uang: {rupiah(s.available)} ({basis})")
    if s.upcoming_bills:
        lines.append(f"Sudah dikurangi tagihan yang jatuh tempo sebelum gajian: {rupiah(s.upcoming_bills)}")
    if s.saved:
        lines.append(f"Sudah dikurangi setoran tabungan siklus ini: {rupiah(s.saved)}")
    if s.avg7:
        if s.available <= 0:
            pace = "perlu ditahan"
        else:
            pace = "⚠️ di atas batas aman" if s.over_pace else "✅ dalam batas aman"
        lines.append(f"Rata-rata 7 harimu: {rupiah(s.avg7)}/hari → {pace}")
    if s.depletion:
        lines.append(f"📉 Dengan laju ini uangmu habis sekitar {tanggal(s.depletion)}, "
                     f"{(s.next_payday - s.depletion).days} hari sebelum gajian.")
    if s.payday_is_default:
        lines.append("(Tanggal gajian belum diatur, dipakai tanggal 1. Atur: /gajian 25)")
    return "\n".join(lines)


# ---------- prediksi anggaran (bulan kalender) ----------
@dataclass
class Forecast:
    name: str
    limit: int
    spent: int
    daily_rate: int
    depletion: date


MIN_DAYS_FOR_FORECAST = 3


def budget_forecasts(db: Database, chat_id: int, today: date) -> list[Forecast]:
    """Anggaran yang bila lajunya berlanjut akan habis sebelum akhir bulan."""
    month_start, month_end = budget.month_bounds(today)
    elapsed = (today - month_start).days + 1
    if elapsed < MIN_DAYS_FOR_FORECAST:
        return []
    out = []
    for s in budget.statuses(db, chat_id, today):
        if s.spent <= 0 or s.spent >= s.limit:
            continue  # belum ada pengeluaran, atau sudah habis (peringatan 100% sudah ada)
        rate = s.spent / elapsed
        depletion = today + timedelta(days=math.floor((s.limit - s.spent) / rate))
        if depletion < month_end - timedelta(days=1):
            out.append(Forecast(budget.label(s.category), s.limit, s.spent, int(rate), depletion))
    return out


def format_forecasts(items: list[Forecast]) -> str:
    return "\n".join(f"📉 {f.name}: dengan laju {rupiah(f.daily_rate)}/hari, anggaran habis sekitar "
                     f"{tanggal(f.depletion)}" for f in items)


# ---------- target tabungan ----------
def monthly_suggestion(goal: Goal, today: date) -> int | None:
    """Setoran per bulan agar target tercapai tepat waktu (bila ada tenggat di masa depan)."""
    if not goal.deadline or goal.deadline <= today or goal.remaining <= 0:
        return None
    months = max(1, (goal.deadline.year - today.year) * 12 + goal.deadline.month - today.month)
    return math.ceil(goal.remaining / months)


def format_goal(goal: Goal, today: date) -> str:
    done = goal.saved >= goal.target
    head = f"{'🎉' if done else '🎯'} {goal.name}: {rupiah(goal.saved)} / {rupiah(goal.target)} ({goal.pct}%)"
    lines = [head, f"   {bar(goal.pct, 12)}"]
    if goal.deadline:
        lines.append(f"   Tenggat {tanggal(goal.deadline)} {goal.deadline.year}")
    suggestion = monthly_suggestion(goal, today)
    if done:
        lines.append("   Target tercapai! 🎉")
    elif suggestion:
        lines.append(f"   Sisa {rupiah(goal.remaining)} → tabung ≈ {rupiah(suggestion)} per bulan")
    elif goal.deadline and goal.deadline <= today:
        lines.append(f"   Tenggat terlewat, sisa {rupiah(goal.remaining)}")
    else:
        lines.append(f"   Sisa {rupiah(goal.remaining)}")
    return "\n".join(lines)


def format_goals(goals: list[Goal], today: date) -> str:
    if not goals:
        return ("Belum ada target tabungan.\nBuat: /target liburan bali 5jt\n"
                "Dengan tenggat: /target dana darurat 10jt sampai 2027-06")
    return "\n\n".join(["🏁 Target tabunganmu", *(format_goal(g, today) for g in goals)])
