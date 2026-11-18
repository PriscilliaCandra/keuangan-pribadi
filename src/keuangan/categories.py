from __future__ import annotations

import re

LAINNYA = "Lainnya"

CATEGORIES = [
    "Makanan & Minuman",
    "Transportasi",
    "Belanja",
    "Tagihan & Utilitas",
    "Langganan & Hiburan",
    "Kesehatan",
    "Pendidikan",
    "Rumah Tangga",
    LAINNYA,
]

# Urutan penting: kategori yang lebih spesifik dicek dulu.
_KEYWORDS: dict[str, list[str]] = {
    "Langganan & Hiburan": [
        "netflix", "spotify", "youtube", "disney", "vidio", "bioskop", "nonton",
        "steam", "game", "langganan", "icloud", "chatgpt", "claude", "hbo",
        "prime video", "karaoke", "konser",
    ],
    "Tagihan & Utilitas": [
        "listrik", "pln", "token", "pdam", "wifi", "internet", "indihome",
        "pulsa", "paket data", "bpjs", "cicilan", "pajak", "kos", "kontrakan",
        "sewa", "asuransi", "tagihan",
    ],
    "Kesehatan": [
        "dokter", "obat", "apotek", "rumah sakit", "klinik", "vitamin", "gym",
        "fitness", "medical check", "lab", "gigi",
    ],
    "Pendidikan": [
        "kursus", "buku", "sekolah", "spp", "kuliah", "udemy", "les", "seminar",
        "workshop",
    ],
    "Transportasi": [
        "bensin", "pertamax", "pertalite", "parkir", "tol", "gojek", "grab",
        "ojek", "ojol", "taksi", "krl", "mrt", "lrt", "transjakarta", "busway",
        "kereta", "pesawat", "servis motor", "servis mobil", "bengkel",
    ],
    "Rumah Tangga": [
        "sabun", "deterjen", "galon", "gas", "lpg", "tisu", "perabot", "laundry",
        "tukang", "pewangi", "sapu", "peralatan",
    ],
    "Belanja": [
        "baju", "sepatu", "tas", "tokopedia", "shopee", "lazada", "belanja",
        "indomaret", "alfamart", "kado", "elektronik", "celana", "jaket",
        "skincare",
    ],
    "Makanan & Minuman": [
        "makan", "minum", "kopi", "sarapan", "lunch", "dinner", "nasi", "ayam",
        "bakso", "mie", "mi", "snack", "jajan", "gofood", "grabfood",
        "shopeefood", "resto", "restoran", "cafe", "kafe", "warung", "roti",
        "boba", "bubur", "sate", "geprek", "sayur", "buah", "beras", "telur",
        "martabak", "pizza", "burger", "teh", "jus", "makan siang",
        "makan malam", "starbucks",
    ],
}

_PATTERNS = {
    cat: re.compile(r"\b(?:" + "|".join(re.escape(k) for k in kws) + r")\b", re.I)
    for cat, kws in _KEYWORDS.items()
}

# Kata yang diketik user di /anggaran -> kategori resmi.
_ALIASES = {
    "makanan": "Makanan & Minuman", "makan": "Makanan & Minuman",
    "minuman": "Makanan & Minuman", "food": "Makanan & Minuman",
    "transport": "Transportasi", "transportasi": "Transportasi",
    "belanja": "Belanja", "shopping": "Belanja",
    "tagihan": "Tagihan & Utilitas", "utilitas": "Tagihan & Utilitas",
    "langganan": "Langganan & Hiburan", "hiburan": "Langganan & Hiburan",
    "kesehatan": "Kesehatan", "pendidikan": "Pendidikan",
    "rumah": "Rumah Tangga", "rt": "Rumah Tangga",
    "lainnya": LAINNYA, "lain": LAINNYA,
}


def guess_category(text: str) -> str | None:
    """Tebak kategori dari kata kunci. None kalau tidak ada yang cocok."""
    for cat, pattern in _PATTERNS.items():
        if pattern.search(text):
            return cat
    return None


# ---------- kategori pemasukan ----------
INCOME_CATEGORIES = ["Gaji", "Bonus & THR", "Freelance & Usaha", "Investasi", LAINNYA]

_INCOME_KEYWORDS: dict[str, list[str]] = {
    "Gaji": ["gaji", "salary", "upah", "payroll"],
    "Bonus & THR": ["bonus", "thr", "insentif", "tunjangan"],
    "Freelance & Usaha": ["freelance", "honor", "komisi", "fee", "proyek", "project", "jualan",
                          "omzet", "usaha", "dagang", "jasa"],
    "Investasi": ["dividen", "bunga", "investasi", "saham", "reksadana", "kupon", "profit"],
}
_INCOME_PATTERNS = {
    cat: re.compile(r"\b(?:" + "|".join(re.escape(k) for k in kws) + r")\b", re.I)
    for cat, kws in _INCOME_KEYWORDS.items()
}
_INCOME_ALIASES = {
    "gaji": "Gaji", "bonus": "Bonus & THR", "thr": "Bonus & THR",
    "freelance": "Freelance & Usaha", "usaha": "Freelance & Usaha", "bisnis": "Freelance & Usaha",
    "investasi": "Investasi", "dividen": "Investasi",
    "lainnya": LAINNYA, "lain": LAINNYA,
}


def guess_income_category(text: str) -> str:
    """Kategori pemasukan dari kata kunci; 'Lainnya' bila tidak ada yang cocok."""
    for cat, pattern in _INCOME_PATTERNS.items():
        if pattern.search(text):
            return cat
    return LAINNYA


def exact_income_category(word: str) -> str | None:
    w = word.strip().lower()
    if w in _INCOME_ALIASES:
        return _INCOME_ALIASES[w]
    return next((c for c in INCOME_CATEGORIES if c.lower() == w), None)


def match_income_category(word: str) -> str | None:
    """Alias, nama persis, atau awalan nama kategori pemasukan ('free' -> Freelance & Usaha)."""
    w = word.strip().lower()
    if not w:
        return None
    exact = exact_income_category(w)
    if exact:
        return exact
    return next((c for c in INCOME_CATEGORIES if c.lower().startswith(w)), None)


def exact_category(word: str) -> str | None:
    """Kategori hanya bila kata persis nama kategori atau aliasnya (tanpa tebakan awalan)."""
    w = word.strip().lower()
    if w in _ALIASES:
        return _ALIASES[w]
    return next((c for c in CATEGORIES if c.lower() == w), None)


def match_category(word: str) -> str | None:
    """Cocokkan ketikan user ke nama kategori resmi (alias atau awalan nama)."""
    w = word.strip().lower()
    if not w:
        return None
    if w in _ALIASES:
        return _ALIASES[w]
    for cat in CATEGORIES:
        if cat.lower().startswith(w):
            return cat
    return None
