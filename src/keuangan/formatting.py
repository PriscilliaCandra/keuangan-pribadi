from __future__ import annotations

from datetime import date

BULAN = [
    "Jan", "Feb", "Mar", "Apr", "Mei", "Jun",
    "Jul", "Agu", "Sep", "Okt", "Nov", "Des",
]


def rupiah(n: int) -> str:
    sign = "-" if n < 0 else ""
    return f"{sign}Rp{abs(int(n)):,}".replace(",", ".")


def bar(pct: float, width: int = 10) -> str:
    filled = max(0, min(width, round(pct / 100 * width)))
    return "█" * filled + "░" * (width - filled)


def tanggal(d: date) -> str:
    return f"{d.day} {BULAN[d.month - 1]}"
