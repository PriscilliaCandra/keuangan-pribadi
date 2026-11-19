"""Validasi masukan dari dashboard web sebelum menyentuh data. Semua batas ada di sini:
isi dari browser tidak pernah dipercaya."""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from .categories import (CATEGORIES, INCOME_CATEGORIES, LAINNYA, exact_category, exact_income_category,
                         guess_category, guess_income_category)
from .parser import ParsedExpense, ParsedIncome

MAX_AMOUNT = 10_000_000_000
MAX_DESC = 200
MAX_CHAT = 500
MAX_PAST_DAYS = 5 * 366
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def clean_text(value: Any, limit: int, what: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{what} harus teks")
    text = re.sub(r"\s+", " ", _CONTROL.sub(" ", value)).strip()
    if len(text) > limit:
        raise ValueError(f"{what} maksimal {limit} karakter")
    return text


def parse_chat(body: dict[str, Any]) -> str:
    text = clean_text(body.get("text"), MAX_CHAT, "pesan")
    if not text:
        raise ValueError("pesan kosong")
    return text


def parse_entry(body: dict[str, Any], today: date) -> ParsedExpense | ParsedIncome:
    kind = body.get("kind")
    if kind not in ("keluar", "masuk"):
        raise ValueError("jenis harus keluar atau masuk")
    amount = body.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, int) or not 0 < amount <= MAX_AMOUNT:
        raise ValueError("jumlah harus bilangan bulat rupiah antara 1 dan 10 miliar")
    description = clean_text(body.get("description", ""), MAX_DESC, "catatan")
    on = today
    raw_date = body.get("date")
    if raw_date not in (None, ""):
        try:
            on = date.fromisoformat(clean_text(raw_date, 10, "tanggal"))
        except ValueError:
            raise ValueError("tanggal tidak valid (YYYY-MM-DD)") from None
        if on > today + timedelta(days=1) or on < today - timedelta(days=MAX_PAST_DAYS):
            raise ValueError("tanggal di luar rentang yang wajar")
    raw_cat = body.get("category")
    category = None
    if raw_cat not in (None, ""):
        name = clean_text(raw_cat, 40, "kategori")
        category = (exact_income_category if kind == "masuk" else exact_category)(name)
        if category is None:
            valid = INCOME_CATEGORIES if kind == "masuk" else CATEGORIES
            raise ValueError("kategori tidak dikenal. Pilihan: " + ", ".join(valid))
    if kind == "masuk":
        description = description or "Pemasukan"
        return ParsedIncome(amount, description, on, category or guess_income_category(description))
    description = description or (category or "Pengeluaran")
    return ParsedExpense(amount, description, category or guess_category(description) or LAINNYA, on)
