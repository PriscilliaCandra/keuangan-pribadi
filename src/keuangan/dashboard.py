"""Dashboard web lokal, BACA-SAJA.

Keamanan:
- Hanya mendengarkan 127.0.0.1 (tidak terjangkau dari jaringan).
- Wajib token (tautan dari /dashboard di Telegram); disimpan sebagai cookie HttpOnly + SameSite=Strict.
- Header Host diperiksa (menangkal DNS rebinding), hanya metode GET/HEAD, tidak ada jalur menulis.
- Berkas statis hanya dari daftar putih; CSP ketat (tanpa skrip/gaya inline, tanpa sumber luar).
- chat_id hanya boleh salah satu dari ALLOWED_CHAT_IDS.
"""
from __future__ import annotations

import hmac
import json
import logging
import secrets
import threading
from collections import defaultdict
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from . import bills as bills_mod
from . import budget
from .agent import resolve_period
from .categories import CATEGORIES, INCOME_CATEGORIES
from .db import Database
from .formatting import BULAN

log = logging.getLogger(__name__)

PERIODS = {
    "bulan_ini": "Bulan ini", "bulan_lalu": "Bulan lalu", "30_hari": "30 hari",
    "3_bulan": "3 bulan", "tahun_ini": "Tahun ini", "semua": "Semua",
}
STATIC_DIR = Path(__file__).parent / "static"
STATIC_FILES = {  # nama yang diminta -> (berkas, content-type)
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
MAX_ROWS = 100
DAILY_MAX_DAYS = 62  # rentang sampai ini ditampilkan per hari, selebihnya per bulan
COOKIE = "kp_session"


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

    budgets = [{"name": budget.label(s.category), "spent": s.spent, "limit": s.limit, "pct": s.pct}
               for s in budget.statuses(db, chat_id, today)]
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
        "bills": {"items": bill_rows,
                  "monthly_total": sum(bills_mod.monthly_cost(b) for b in active_bills)},
        "categories": {"expense": CATEGORIES, "income": INCOME_CATEGORIES},
        "periods": PERIODS,
    }


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


# ---------- token ----------
def load_or_create_token(path: str | Path) -> str:
    """Token akses dashboard: dibaca dari berkas, atau dibuat acak sekali lalu disimpan."""
    p = Path(path)
    if p.exists():
        token = p.read_text(encoding="utf-8").strip()
        if len(token) >= 16:
            return token
    p.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    p.write_text(token, encoding="utf-8")
    return token


# ---------- server ----------
class _Handler(BaseHTTPRequestHandler):
    server: "DashboardServer"  # type: ignore[assignment]
    protocol_version = "HTTP/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # URL bisa memuat token; jangan dicatat

    # -- util --
    def _send(self, status: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'self'; style-src 'self'; "
                         "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _cookie_token(self) -> str:
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE:
                return value
        return ""

    def _authorized(self) -> bool:
        return hmac.compare_digest(self._cookie_token().encode(), self.server.token.encode())

    # -- routing --
    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        self._json(405, {"error": "hanya baca-saja"})

    do_PUT = do_DELETE = do_PATCH = do_POST  # noqa: N815

    def do_GET(self) -> None:  # noqa: N802
        if self.headers.get("Host", "") not in self.server.allowed_hosts:
            self._json(403, {"error": "host tidak diizinkan"})
            return
        url = urlsplit(self.path)
        query = parse_qs(url.query)

        supplied = (query.get("t") or [""])[0]
        if supplied:  # tautan dari Telegram: tukar token dengan cookie lalu bersihkan URL
            if hmac.compare_digest(supplied.encode(), self.server.token.encode()):
                self._send(302, b"", "text/plain", {
                    "Location": url.path or "/",
                    "Set-Cookie": f"{COOKIE}={self.server.token}; HttpOnly; SameSite=Strict; Path=/"})
            else:
                self._json(401, {"error": "token salah"})
            return
        if not self._authorized():
            self._json(401, {"error": "butuh token. Buka tautan dari perintah /dashboard di Telegram."})
            return

        if url.path in STATIC_FILES:
            name, ctype = STATIC_FILES[url.path]
            self._send(200, (STATIC_DIR / name).read_bytes(), ctype)
        elif url.path == "/api/summary":
            self._api(lambda chat: build_summary(self.server.db, chat, self._arg(query, "period", "bulan_ini"),
                                                 self.server.clock()), query)
        elif url.path == "/api/transactions":
            self._api(lambda chat: list_transactions(
                self.server.db, chat, self._arg(query, "period", "bulan_ini"), self.server.clock(),
                self._arg(query, "kind", "semua"), self._arg(query, "category", ""),
                self._arg(query, "q", ""), self._int(query, "limit", 25), self._int(query, "offset", 0)),
                query)
        else:
            self._json(404, {"error": "tidak ditemukan"})

    @staticmethod
    def _arg(query: dict[str, list[str]], name: str, default: str) -> str:
        return (query.get(name) or [default])[0]

    @staticmethod
    def _int(query: dict[str, list[str]], name: str, default: int) -> int:
        try:
            return int((query.get(name) or [default])[0])
        except ValueError:
            return default

    def _api(self, fn: Callable[[int], dict[str, Any]], query: dict[str, list[str]]) -> None:
        chats = self.server.chat_ids
        raw = (query.get("chat") or [""])[0]
        if not chats:
            self._json(403, {"error": "ALLOWED_CHAT_IDS kosong"})
            return
        try:
            chat = int(raw) if raw else min(chats)
        except ValueError:
            self._json(400, {"error": "chat tidak valid"})
            return
        if chat not in chats:  # tidak boleh membaca data chat lain
            self._json(403, {"error": "chat tidak diizinkan"})
            return
        try:
            self._json(200, fn(chat))
        except ValueError as exc:
            self._json(400, {"error": str(exc)})


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, db: Database, chat_ids: frozenset[int], clock: Callable[[], date],
                 token: str, port: int = 8765, host: str = "127.0.0.1"):
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("dashboard hanya boleh mendengarkan alamat lokal")
        super().__init__((host, port), _Handler)
        self.db, self.chat_ids, self.clock, self.token = db, chat_ids, clock, token
        p = self.server_port
        self.allowed_hosts = {f"localhost:{p}", f"127.0.0.1:{p}", f"[::1]:{p}"}
        self._thread: threading.Thread | None = None

    def url(self) -> str:
        return f"http://localhost:{self.server_port}/?t={self.token}"

    def start(self) -> None:
        self._thread = threading.Thread(target=self.serve_forever, name="dashboard", daemon=True)
        self._thread.start()
        log.info("Dashboard aktif di http://localhost:%d/ (hanya laptop ini)", self.server_port)

    def stop(self) -> None:
        self.shutdown()
        self.server_close()
