"""Kalkulasi data dashboard (murni: tanpa HTTP). Server ada di dashboard_server.py."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from . import bills as bills_mod
from . import budget
from . import debts as debts_mod
from . import insights as insights_mod
from . import routines as routines_mod
from . import plans as plans_mod
from .agent import resolve_period
from .categories import CATEGORIES, INCOME_CATEGORIES
from .db import Database
from .formatting import BULAN

PERIODS = {
    "bulan_ini": "Bulan ini", "bulan_lalu": "Bulan lalu", "30_hari": "30 hari",
    "3_bulan": "3 bulan", "tahun_ini": "Tahun ini", "semua": "Semua",
}
MAX_ROWS = 100
DAILY_MAX_DAYS = 62  # rentang sampai ini ditampilkan per hari, selebihnya per bulan


# ---------- kalkulasi data (murni, mudah dites) ----------
def _pct_change(now: int, before: int) -> int | None:
    return None if before <= 0 else round((now - before) * 100 / before)


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _next_month(d: date) -> date:
    return (d.replace(day=28) + timedelta(days=4)).replace(day=1)


def _series(expenses, incomes, start: date, end: date) -> tuple[str, list[dict[str, Any]]]:
    daily = (end - start).days <= DAILY_MAX_DAYS
    key = (lambda d: d.isoformat()) if daily else (lambda d: d.strftime("%Y-%m"))
    spent: dict[str, int] = defaultdict(int)
    earned: dict[str, int] = defaultdict(int)
    for e in expenses:
        spent[key(e.spent_on)] += e.amount
    for i in incomes:
        earned[key(i.received_on)] += i.amount

    points = []
    cursor = start if daily else _month_start(start)
    while cursor < end:
        k = key(cursor)
        label = (f"{cursor.day} {BULAN[cursor.month - 1]}" if daily
                 else f"{BULAN[cursor.month - 1]} {cursor.year}")
        points.append({"key": k, "label": label, "income": earned.get(k, 0),
                       "expense": spent.get(k, 0)})
        cursor = cursor + timedelta(days=1) if daily else _next_month(cursor)
    return ("day" if daily else "month"), points


def _breakdown(rows: list[tuple[str, int]], total: int) -> list[dict[str, Any]]:
    sums: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for name, amount in rows:
        sums[name][0] += amount
        sums[name][1] += 1
    return [{"name": n, "total": t, "count": c, "pct": round(t * 100 / total) if total else 0}
            for n, (t, c) in sorted(sums.items(), key=lambda kv: -kv[1][0])]


def build_summary(db: Database, chat_id: int, period: str, today: date) -> dict[str, Any]:
    if period not in PERIODS:
        raise ValueError(f"periode tidak dikenal: {period}")
    start, end = resolve_period(period, today)
    end = min(end, today + timedelta(days=1))  # hari yang belum terjadi tidak ditampilkan
    if period == "semua":
        firsts = [x.spent_on for x in db.expenses_between(chat_id, start, end)[:1]]
        firsts += [x.received_on for x in db.incomes_between(chat_id, start, end)[:1]]
        start = min(firsts) if firsts else _month_start(today)
    expenses = db.expenses_between(chat_id, start, end)
    incomes = db.incomes_between(chat_id, start, end)

    spent = sum(e.amount for e in expenses)
    earned = sum(i.amount for i in incomes)
    span = end - start
    prev_spent = db.sum_between(chat_id, start - span, start)
    prev_earned = db.sum_income_between(chat_id, start - span, start)
    granularity, series = _series(expenses, incomes, start, end)
    last_day = end - timedelta(days=1)

    predicted = {f.name: f.depletion.isoformat() for f in plans_mod.budget_forecasts(db, chat_id, today)}
    budgets = [{"name": budget.label(s.category), "spent": s.spent, "limit": s.limit, "pct": s.pct,
                "forecast": predicted.get(budget.label(s.category))}
               for s in budget.statuses(db, chat_id, today)]
    plans = plans_mod.PlanStore(db)
    safe = plans_mod.safe_daily(db, plans, chat_id, today)
    goals = [{"name": g.name, "saved": g.saved, "target": g.target, "pct": g.pct,
              "deadline": g.deadline.isoformat() if g.deadline else None,
              "monthly": plans_mod.monthly_suggestion(g, today)} for g in plans.list_goals(chat_id)]
    bill_rows = []
    active_bills = db.list_bills(chat_id)
    for b in sorted(active_bills, key=lambda x: bills_mod.next_due(x, today)):
        due = bills_mod.next_due(b, today)
        bill_rows.append({"name": b.name, "kind": b.kind, "amount": b.amount,
                          "cycle": b.cycle, "due": due.isoformat(), "days_left": (due - today).days})

    return {
        "period": period, "label": PERIODS[period],
        "from": start.isoformat(), "to": last_day.isoformat(), "today": today.isoformat(),
        "kpi": {
            "income": earned, "expense": spent, "net": earned - spent,
            "savings_rate": round((earned - spent) * 100 / earned) if earned > 0 else None,
            "transactions": len(expenses) + len(incomes),
            "income_change": _pct_change(earned, prev_earned),
            "expense_change": _pct_change(spent, prev_spent),
        },
        "granularity": granularity,
        "series": series,
        "expense_by_category": _breakdown([(e.category, e.amount) for e in expenses], spent),
        "income_by_category": _breakdown([(i.category, i.amount) for i in incomes], earned),
        "top_expenses": [{"date": e.spent_on.isoformat(), "amount": e.amount,
                          "category": e.category, "note": e.description}
                         for e in sorted(expenses, key=lambda x: -x.amount)[:5]],
        "budgets": budgets,
        "safe_daily": None if safe is None else {
            "mode": safe.mode, "per_day": safe.per_day, "available": safe.available,
            "days_left": safe.days_left, "next_payday": safe.next_payday.isoformat(),
            "avg7": safe.avg7, "over_pace": safe.over_pace and safe.available > 0,
            "depletion": safe.depletion.isoformat() if safe.depletion else None,
            "payday_is_default": safe.payday_is_default, "upcoming_bills": safe.upcoming_bills,
            "saved": safe.saved},
        "goals": goals,
        "debts": _debts_payload(db, chat_id, today),
        "insights": [i.text for i in insights_mod.build_insights(db, chat_id, today)],
        "recurring": [{"name": r.name, "kind": r.kind, "amount": r.amount, "category": r.category,
                       "day": r.day, "active": r.active}
                      for r in routines_mod.RecurringStore(db).list(chat_id)],
        "bills": {"items": bill_rows,
                  "monthly_total": sum(bills_mod.monthly_cost(b) for b in active_bills)},
        "categories": {"expense": CATEGORIES, "income": INCOME_CATEGORIES},
        "periods": PERIODS,
    }


def _debts_payload(db: Database, chat_id: int, today: date) -> dict[str, Any]:
    store = debts_mod.DebtStore(db)
    debts = store.list(chat_id)
    owed_to_me, owed_by_me = store.totals(chat_id)

    def rows(direction: str) -> list[dict[str, Any]]:
        return [{"person": person, "amount": total, "notes": notes[:2],
                 "due": due.isoformat() if due else None, "overdue": bool(due and due < today)}
                for person, total, notes, due in debts_mod._group(debts, direction)[:8]]

    return {"owed_to_me": owed_to_me, "owed_by_me": owed_by_me,
            "to_me": rows(debts_mod.PIUTANG), "by_me": rows(debts_mod.UTANG)}


def list_transactions(db: Database, chat_id: int, period: str, today: date, kind: str = "semua",
                      category: str = "", query: str = "", limit: int = 25,
                      offset: int = 0) -> dict[str, Any]:
    if period not in PERIODS:
        raise ValueError(f"periode tidak dikenal: {period}")
    if kind not in ("semua", "keluar", "masuk"):
        raise ValueError("jenis harus semua, keluar, atau masuk")
    start, end = resolve_period(period, today)
    rows: list[dict[str, Any]] = []
    if kind in ("semua", "keluar"):
        rows += [{"id": str(e.id), "date": e.spent_on.isoformat(), "kind": "keluar",
                  "amount": e.amount, "category": e.category, "note": e.description,
                  "source": e.source, "_k": (e.spent_on, e.id)}
                 for e in db.expenses_between(chat_id, start, end)]
    if kind in ("semua", "masuk"):
        rows += [{"id": f"m{i.id}", "date": i.received_on.isoformat(), "kind": "masuk",
                  "amount": i.amount, "category": i.category, "note": i.description,
                  "source": i.source, "_k": (i.received_on, i.id)}
                 for i in db.incomes_between(chat_id, start, end)]
    category, query = category.strip().lower(), query.strip().lower()[:60]
    if category:
        rows = [r for r in rows if r["category"].lower() == category]
    if query:
        rows = [r for r in rows if query in r["note"].lower() or query in r["category"].lower()]
    rows.sort(key=lambda r: r["_k"], reverse=True)
    limit = max(1, min(MAX_ROWS, limit))
    offset = max(0, offset)
    page = [{k: v for k, v in r.items() if k != "_k"} for r in rows[offset: offset + limit]]
    return {"total": len(rows), "limit": limit, "offset": offset, "rows": page}
