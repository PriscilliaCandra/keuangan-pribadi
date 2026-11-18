"""Catatan rutin: pengeluaran/pemasukan yang tercatat otomatis tiap bulan (kos, cicilan, gaji)."""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date

from .categories import LAINNYA, guess_category, guess_income_category
from .db import Database
from .formatting import rupiah
from .parser import find_amount, is_income_text

KELUAR, MASUK = "keluar", "masuk"
MAX_RECURRING = 30
MAX_AMOUNT = 10_000_000_000


def effective_day(day: int, year: int, month: int) -> int:
    """Tanggal 31 di bulan pendek jatuh di hari terakhir bulan itu."""
    return min(day, calendar.monthrange(year, month)[1])


def month_key(d: date) -> str:
    return d.strftime("%Y-%m")


@dataclass
class Recurring:
    id: int
    chat_id: int
    name: str
    kind: str
    amount: int
    category: str
    day: int
    active: bool
    last_run_month: str | None
    created_on: date

    @property
    def key(self) -> str:
        return self.name.strip().lower()


class RecurringStore:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _rec(row) -> Recurring:
        return Recurring(row["id"], row["chat_id"], row["name"], row["kind"], row["amount"],
                         row["category"], row["day"], bool(row["active"]), row["last_run_month"],
                         date.fromisoformat(row["created_on"]))

    def list(self, chat_id: int, active_only: bool = False) -> list[Recurring]:
        sql = "SELECT * FROM recurring WHERE chat_id=?" + (" AND active=1" if active_only else "")
        return [self._rec(r) for r in self.db.query(sql + " ORDER BY day, id", (chat_id,))]

    def find(self, chat_id: int, text: str) -> Recurring | None:
        key = text.strip().lower()
        if not key:
            return None
        items = self.list(chat_id)
        return next((r for r in items if r.key == key), None) or \
            next((r for r in items if key in r.key or r.key in key), None)

    def add(self, chat_id: int, name: str, kind: str, amount: int, category: str, day: int,
            today: date) -> tuple[Recurring, bool]:
        """Buat catatan rutin, atau perbarui yang namanya sama (dan aktifkan lagi).

        Bulan ini TIDAK dicatat otomatis bila tanggalnya sudah lewat: kemungkinan besar kamu sudah
        membayarnya, dan mencatat lagi akan menggandakan.
        """
        if kind not in (KELUAR, MASUK):
            raise ValueError("jenis harus keluar atau masuk")
        if not 1 <= day <= 31:
            raise ValueError("tanggal harus 1-31")
        if not 0 < amount <= MAX_AMOUNT:
            raise ValueError("jumlah tidak wajar")
        name = name.strip()[:60]
        if not name:
            raise ValueError("nama kosong")
        key = name.lower()
        existing = next((r for r in self.list(chat_id) if r.key == key), None)
        current = month_key(today)
        past = effective_day(day, today.year, today.month) <= today.day
        if existing:
            last = existing.last_run_month if existing.last_run_month == current else (current if past else None)
            self.db.execute(
                "UPDATE recurring SET kind=?, amount=?, category=?, day=?, active=1, last_run_month=?"
                " WHERE id=? AND chat_id=?", (kind, amount, category, day, last, existing.id, chat_id))
            return self.get(chat_id, existing.id), False
        if len(self.list(chat_id)) >= MAX_RECURRING:
            raise ValueError(f"maksimal {MAX_RECURRING} catatan rutin")
        cur = self.db.execute(
            "INSERT INTO recurring (chat_id, name, name_key, kind, amount, category, day, last_run_month,"
            " created_on) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (chat_id, name, key, kind, amount, category, day, current if past else None, today.isoformat()))
        return self.get(chat_id, int(cur.lastrowid)), True

    def get(self, chat_id: int, rec_id: int) -> Recurring:
        return self._rec(self.db.query("SELECT * FROM recurring WHERE id=? AND chat_id=?", (rec_id, chat_id))[0])

    def set_active(self, chat_id: int, rec_id: int, active: bool) -> bool:
        return self.db.execute("UPDATE recurring SET active=? WHERE id=? AND chat_id=?",
                               (int(active), rec_id, chat_id)).rowcount == 1

    def delete(self, chat_id: int, rec_id: int) -> bool:
        return self.db.execute("DELETE FROM recurring WHERE id=? AND chat_id=?", (rec_id, chat_id)).rowcount == 1

    def due(self, chat_id: int, today: date) -> list[tuple[Recurring, date]]:
        """Yang harus dicatat bulan ini: aktif, belum dijalankan bulan ini, tanggalnya sudah tiba."""
        current = month_key(today)
        out = []
        for r in self.list(chat_id, active_only=True):
            day = effective_day(r.day, today.year, today.month)
            if r.last_run_month != current and day <= today.day:
                out.append((r, date(today.year, today.month, day)))
        return out

    def claim(self, chat_id: int, rec_id: int, today: date) -> bool:
        """Tandai sudah dijalankan bulan ini. Atomik: dua proses tak bisa sama-sama mengklaim."""
        current = month_key(today)
        cur = self.db.execute(
            "UPDATE recurring SET last_run_month=? WHERE id=? AND chat_id=? AND active=1"
            " AND (last_run_month IS NULL OR last_run_month != ?)", (current, rec_id, chat_id, current))
        return cur.rowcount == 1


# ---------- pemahaman perintah & kalimat ----------
@dataclass
class RecurringSpec:
    name: str
    amount: int
    day: int
    kind: str
    category: str


_DAY = re.compile(r"(?i)\b(?:tgl|tanggal)\s*(\d{1,2})\b")
_KIND_WORD = re.compile(r"(?i)\b(?:masuk|pemasukan)\b")
_EVERY_MONTH = re.compile(r"(?i)\b(?:tiap|setiap)\s+(?:bulan|bln)\b[\s,]*")
_EVERY_DAY = re.compile(r"(?i)\b(?:tiap|setiap)\s+(?:tanggal|tgl)\s*(\d{1,2})\b")
USAGE = ("Format: /rutin <nama> <jumlah> tgl <1-31> [masuk]\n"
         "Contoh: /rutin kos 1,5jt tgl 1 · /rutin gaji 8jt tgl 25 masuk · /rutin cicilan motor 850rb tgl 10")


def parse_recurring_args(args: str) -> RecurringSpec | str:
    """'kos 1,5jt tgl 1' -> RecurringSpec, atau str berisi pesan galat."""
    text = f" {args.strip()} "
    day = None
    if m := _DAY.search(text):
        day = int(m.group(1))
        text = text[: m.start()] + " " + text[m.end():]
    kind = KELUAR
    if _KIND_WORD.search(text):
        kind = MASUK
        text = _KIND_WORD.sub(" ", text)
    found = find_amount(text)
    if not found:
        return "Jumlahnya tidak terbaca.\n" + USAGE
    s, e = found[1]
    name = re.sub(r"\s+", " ", _EVERY_MONTH.sub(" ", text[:s] + " " + text[e:])).strip(" -:,.")
    if not name:
        return "Namanya kosong.\n" + USAGE
    if day is None:
        return "Tanggalnya belum ada. Tambahkan, mis. 'tgl 1'.\n" + USAGE
    if not 1 <= day <= 31:
        return "Tanggal harus 1-31.\n" + USAGE
    if found[0] > MAX_AMOUNT:
        return "Jumlahnya tidak wajar."
    if kind == KELUAR and is_income_text(name):
        kind = MASUK
    category = guess_income_category(name) if kind == MASUK else (guess_category(name) or LAINNYA)
    return RecurringSpec(name[:60], found[0], day, kind, category)


def recurring_args_from_text(text: str) -> str | None:
    """'kos 1,5jt tiap bulan tgl 1' / 'netflix 54rb setiap tanggal 5' -> argumen /rutin; None bila bukan.
    Wajib memuat nominal supaya pertanyaan seperti 'berapa pengeluaran tiap bulan?' tak salah dikira."""
    if find_amount(text) is None:
        return None
    if m := _EVERY_DAY.search(text):
        return f"{text[: m.start()]} tgl {m.group(1)} {text[m.end():]}"
    if _EVERY_MONTH.search(text):
        return _EVERY_MONTH.sub(" ", text)
    return None


def format_spec_line(r: Recurring) -> str:
    sign = "+" if r.kind == MASUK else ""
    state = "" if r.active else " ⏸ dijeda"
    return f"• {r.name} — {sign}{rupiah(r.amount)} · tiap tgl {r.day} · {r.category}{state}"


def format_list(items: list[Recurring]) -> str:
    if not items:
        return ("Belum ada catatan rutin.\nBuat: /rutin kos 1,5jt tgl 1  (atau ketik: \"kos 1,5jt tiap bulan tgl 1\")")
    return "\n".join(["🔁 Catatan rutin (tercatat otomatis tiap bulan)", *map(format_spec_line, items),
                      "\nJeda: /rutin jeda <nama> · Lanjut: /rutin lanjut <nama> · Hapus: /rutin hapus <nama>"])
