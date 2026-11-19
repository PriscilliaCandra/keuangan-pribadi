"""Parser pesan pengeluaran bahasa Indonesia: "makan siang 35rb", "kopi 1,5k kemarin"."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from .categories import guess_category, guess_income_category

_NUM = re.compile(
    r"(?<![\w.,])(rp\.?\s*)?(\d{1,3}(?:[.,]\d{3})+|\d+(?:[.,]\d+)?)\s*(juta|jt|ribu|rb|k)?(?![a-z\d])",
    re.I,
)
_GROUPED = re.compile(r"\d{1,3}(?:[.,]\d{3})+")
_MULT = {"juta": 1_000_000, "jt": 1_000_000, "ribu": 1_000, "rb": 1_000, "k": 1_000}

_YESTERDAY = re.compile(r"\b(kemarin|kemaren)\b", re.I)
_DAYS_AGO = re.compile(r"\b(\d{1,2})\s*hari\s*(?:yang\s*)?lalu\b", re.I)
_TODAY = re.compile(r"\b(hari\s*ini|tadi|barusan)\b", re.I)


@dataclass
class ParsedExpense:
    amount: int
    description: str
    category: str | None
    spent_on: date


def _to_number(raw: str) -> float:
    if _GROUPED.fullmatch(raw):
        return float(re.sub(r"[.,]", "", raw))
    return float(raw.replace(",", "."))


# Bahasa sehari-hari untuk uang. "2 goceng" = 10 ribu.
_SLANG = {"seceng": 1_000, "seribu": 1_000, "goceng": 5_000, "ceban": 10_000, "noban": 20_000,
          "gocap": 50_000, "cepek": 100_000, "sejuta": 1_000_000}
_SLANG_RE = re.compile(r"(?<!\w)(?:(\d{1,3})\s*)?(" + "|".join(_SLANG) + r")(?![a-z\d])", re.I)


def find_amount(text: str) -> tuple[int, tuple[int, int]] | None:
    """Cari nominal rupiah. Mengembalikan (nilai, (awal, akhir)) atau None.

    Prioritas: angka bertanda (Rp / rb / k / jt / goceng / ceban ...) paling akhir, lalu angka >= 1000.
    Angka kecil tanpa penanda ("2 kopi") sengaja diabaikan.
    """
    marked: list[tuple[int, tuple[int, int]]] = []
    plain: list[tuple[int, tuple[int, int]]] = []
    for m in _SLANG_RE.finditer(text):
        marked.append((int(m.group(1) or 1) * _SLANG[m.group(2).lower()], m.span()))
    for m in _NUM.finditer(text):
        rp, raw, unit = m.group(1), m.group(2), m.group(3)
        value = _to_number(raw)
        if unit:
            value *= _MULT[unit.lower()]
        amount = int(round(value))
        if amount <= 0:
            continue
        if rp or unit:
            marked.append((amount, m.span()))
        elif amount >= 1000:
            plain.append((amount, m.span()))
    if marked:
        return max(marked, key=lambda c: c[1][0])  # yang paling akhir dalam kalimat
    if plain:
        return plain[-1]
    return None


def _extract_date(text: str, today: date) -> tuple[date, str]:
    spent_on = today
    m = _YESTERDAY.search(text)
    if m:
        spent_on = today - timedelta(days=1)
        return spent_on, text[: m.start()] + " " + text[m.end():]
    m = _DAYS_AGO.search(text)
    if m:
        spent_on = today - timedelta(days=int(m.group(1)))
        return spent_on, text[: m.start()] + " " + text[m.end():]
    m = _TODAY.search(text)
    if m:
        return spent_on, text[: m.start()] + " " + text[m.end():]
    return spent_on, text


def parse_expense(text: str, today: date) -> ParsedExpense | None:
    """Ubah teks bebas jadi pengeluaran. None kalau tidak ada nominal."""
    spent_on, rest = _extract_date(text, today)
    found = find_amount(rest)
    if found is None:
        return None
    amount, (start, end) = found
    rest = rest[:start] + " " + rest[end:]
    description = re.sub(r"\s+", " ", rest).strip(" -:,.")
    description = description or "Pengeluaran"
    return ParsedExpense(
        amount=amount,
        description=description[:120],
        category=guess_category(description),
        spent_on=spent_on,
    )


# ---------- pemasukan ----------
_INCOME_START = re.compile(
    r"^\s*(?:\+\s*|(?:gaji|bonus|thr|pemasukan|income|diterima|komisi|honor|cashback|refund|"
    r"dividen|pendapatan|uang\s+masuk|transfer\s+masuk)\b)",
    re.I,
)


@dataclass
class ParsedIncome:
    amount: int
    description: str
    received_on: date
    category: str = "Lainnya"


def is_income_text(text: str) -> bool:
    return bool(_INCOME_START.match(text))


def parse_income(text: str, today: date, force: bool = False) -> ParsedIncome | None:
    """'gaji 8jt', '+500rb freelance', 'bonus 1,5jt kemarin'. `force` untuk perintah /masuk."""
    if not force and not is_income_text(text):
        return None
    received_on, rest = _extract_date(text, today)
    found = find_amount(rest)
    if found is None:
        return None
    amount, (start, end) = found
    rest = rest[:start] + " " + rest[end:]
    rest = re.sub(r"^\s*\+\s*", "", rest)
    description = re.sub(r"\s+", " ", rest).strip(" -:,.") or "Pemasukan"
    return ParsedIncome(amount, description[:120], received_on,
                        guess_income_category(text))  # kata kunci dicari di seluruh kalimat


# ---------- tanggal untuk /edit ----------
_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_DMY = re.compile(r"^(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?$")


def parse_date_text(text: str, today: date) -> date | None:
    """'kemarin', '3 hari lalu', '2026-11-05', '5/11', '05-11-2026'. Tidak boleh di masa depan."""
    t = text.strip().lower()
    result: date | None = None
    try:
        if t in ("hari ini", "hariini", "today"):
            result = today
        elif t in ("kemarin", "kemaren"):
            result = today - timedelta(days=1)
        elif m := re.fullmatch(r"(\d{1,3})\s*hari\s*(?:yang\s*)?lalu", t):
            result = today - timedelta(days=int(m.group(1)))
        elif m := _ISO.match(t):
            result = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        elif m := _DMY.match(t):
            day, month, year = int(m.group(1)), int(m.group(2)), m.group(3)
            if year:
                y = int(year)
                result = date(y + 2000 if y < 100 else y, month, day)
            else:
                result = date(today.year, month, day)
                if result > today:
                    result = date(today.year - 1, month, day)
    except ValueError:
        return None
    return result if result and result <= today else None


# ---------- pertanyaan ----------
_QUESTION_START = re.compile(
    r"^\s*(?:berapa|apa|apakah|kapan|siapa|bagaimana|gimana|kenapa|mengapa|tampilkan|"
    r"tunjukkan|bandingkan|rata-rata|(?:bulan|hari|minggu|kategori|toko)\s+apa)\b",
    re.I,
)


# Permintaan ("jelaskan detail pengeluaranku"). Kelompok kedua bisa juga awal sebuah catatan
# ("cek kesehatan 200rb"), jadi hanya dianggap permintaan bila kalimatnya tanpa nominal.
_REQUEST_START = re.compile(
    r"^\s*(?:tolong\s+|coba\s+|boleh\s+)?(?:jelaskan|jelasin|ceritakan|ceritain|rincikan|rinci|rangkum|"
    r"ringkas|analisis|analisa|sebutkan|sebutin)\b", re.I)
_SOFT_REQUEST_START = re.compile(
    r"^\s*(?:tolong\s+|coba\s+|boleh\s+)?(?:lihat|liat|cek|hitung|kasih\s+(?:tahu|tau)|beri\s+tahu|"
    r"detail|rincian|laporan)\b", re.I)


def is_question(text: str) -> bool:
    """Pertanyaan/permintaan tentang data. Kalimat bernominal berakhiran '?' tetap dianggap catatan."""
    if _QUESTION_START.match(text) or _REQUEST_START.match(text):
        return True
    if _SOFT_REQUEST_START.match(text) and find_amount(text) is None:
        return True
    return text.rstrip().endswith("?") and find_amount(text) is None


# ---------- koreksi & hapus dengan bahasa biasa ----------
_EDIT_VERB = re.compile(
    r"^\s*(?:tolong\s+)?(ubah|ganti|koreksi|edit|harusnya|seharusnya|ternyata|maksudnya)\b[\s,:]*", re.I)
_DIRECT_VERBS = ("harusnya", "seharusnya", "ternyata", "maksudnya")  # nilai langsung setelahnya
# "gaji kemarin ternyata 7 juta": <catatan yang dimaksud> <penanda> <nilai baru>
_MID_MARKER = re.compile(
    r"^(?P<subject>.+?)\s+(?:ternyata|maksudnya|harusnya|seharusnya)\b[\s,:]*(?P<value>.+)$", re.I)
_HINT_STOP = re.compile(
    r"^(?:yang|tadi|terakhir|barusan|itu|tuh|kemarin|kemaren|hari|ini|lalu|dong|deh|ya|aja|saja|"
    r"nya|tolong|eh|oh|ok|oke|sih|kok|kan|loh|lho)$", re.I)
_EDIT_NYA = re.compile(r"^\s*(?:kategori|nominal|jumlah|harga|tanggal|catatan)nya\b", re.I)
_FIELD_WORD = re.compile(
    r"\b(kategori|nominal|jumlah|harga|tanggal|tgl|catatan|deskripsi|nama)(?:nya)?\b"
    r"[\s:]*(?:jadi\b|menjadi\b|ke\b)?\s*", re.I)
_FILLER = re.compile(r"\b(?:yang\s+)?(?:tadi|terakhir|barusan|itu)\b|\b(?:dong|deh|ya|aja|saja)\b", re.I)
_CONNECT = re.compile(r"^(?:jadi|menjadi|ke)\b\s*", re.I)
_DATE_TOKEN = re.compile(
    r"\b(?:kemarin|kemaren|hari\s*ini|\d{1,3}\s*hari\s*(?:yang\s*)?lalu|\d{4}-\d{1,2}-\d{1,2}|"
    r"\d{1,2}/\d{1,2}(?:/\d{2,4})?)(?!\w)", re.I)
_DELETE = re.compile(r"^\s*(?:tolong\s+)?(?:hapus|batalkan|batalin|undo)\b(?P<rest>.*)$", re.I)
_TARGET_WORD = re.compile(r"\b(?:tadi|terakhir|barusan|itu)\b", re.I)


def _field_pairs(rest: str) -> list[tuple[str, str]]:
    matches = list(_FIELD_WORD.finditer(rest))
    pairs: list[tuple[str, str]] = []
    prefix = rest[: matches[0].start()].strip(" ,.:")
    if prefix:  # "40rb kategori transport": bagian sebelum kata 'kategori' berupa nilai murni
        pure = _pure_value_pairs(prefix)
        if not pure:
            return []
        pairs += pure
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(rest)
        value = re.sub(r"\s+(?:dan|serta|lalu)$", "", rest[m.end():end].strip(" ,."), flags=re.I)
        if not value:
            return []
        pairs.append((m.group(1).lower(), value))
    return pairs


def _pure_value_pairs(rest: str) -> list[tuple[str, str]]:
    """'40rb kemarin transportasi' -> nominal, tanggal, kategori. Kata tak dikenal = bukan koreksi."""
    from .categories import exact_category, exact_income_category

    pairs: list[tuple[str, str]] = []
    remaining = rest
    d = _DATE_TOKEN.search(remaining)
    if d:
        pairs.append(("tanggal", d.group(0)))
        remaining = remaining[: d.start()] + " " + remaining[d.end():]
    found = find_amount(remaining)
    if found:
        start, end = found[1]
        pairs.append(("nominal", remaining[start:end]))
        remaining = remaining[:start] + " " + remaining[end:]
    remaining = re.sub(r"\s+", " ", remaining).strip(" ,.:")
    if remaining:
        # Nilai disimpan apa adanya; apply_edits memetakannya sesuai jenis catatan
        # (kategori pengeluaran atau pemasukan).
        if exact_category(remaining) is None and exact_income_category(remaining) is None:
            return []
        pairs.append(("kategori", remaining))
    return pairs


def parse_quick_edit(text: str, in_reply: bool) -> list[tuple[str, str]] | None:
    """Koreksi tanpa /edit. Mengembalikan [(bagian, nilai)] atau None bila bukan koreksi.

    - Sebagai balasan ke pesan catatan (`in_reply`): cukup nilai murni, mis. 'transportasi',
      '40rb', 'kemarin'.
    - Tanpa balasan: harus eksplisit ('ubah jadi 40rb', 'kategorinya transport', 'harusnya 40rb').
      'ganti oli 150rb' BUKAN koreksi (itu pengeluaran).
    """
    t = text.strip()
    verb_m = _EDIT_VERB.match(t)
    verb = verb_m.group(1).lower() if verb_m else None
    rest = t[verb_m.end():] if verb_m else t
    rest = re.sub(r"\s+", " ", _FILLER.sub(" ", rest)).strip(" ,.:")
    connect = _CONNECT.match(rest)
    if connect:
        rest = rest[connect.end():].strip(" ,.:")
    explicit = bool(
        _EDIT_NYA.match(t)
        or verb in _DIRECT_VERBS
        or (verb and (connect or _FIELD_WORD.match(rest)))
    )
    if not rest or not (in_reply or explicit):
        return None
    pairs = _field_pairs(rest) if _FIELD_WORD.search(rest) else _pure_value_pairs(rest)
    return pairs or None


@dataclass
class QuickEdit:
    pairs: list[tuple[str, str]]
    hint: list[str]  # kata yang menunjuk catatan ("gaji"); kosong = catatan terakhir / yang dibalas


def _hint_words(subject: str) -> list[str]:
    words = re.findall(r"[a-zA-Z]{3,}", _DATE_TOKEN.sub(" ", subject))
    return [w.lower() for w in words if not _HINT_STOP.match(w)]


def parse_quick_edit_full(text: str, in_reply: bool) -> QuickEdit | None:
    """Seperti parse_quick_edit, ditambah bentuk '<catatan> ternyata <nilai>'.

    'gaji kemarin ternyata 7 juta' -> ubah nominal catatan terbaru yang mengandung 'gaji'.
    Kalau nilai setelah penanda bukan nilai murni ('kopi ternyata mahal 50rb'), ini bukan koreksi.
    """
    m = _MID_MARKER.match(text.strip())
    if m and not _EDIT_VERB.match(text):
        value = re.sub(r"\s+", " ", _FILLER.sub(" ", m.group("value"))).strip(" ,.:")
        connect = _CONNECT.match(value)
        if connect:
            value = value[connect.end():].strip(" ,.:")
        pairs = _field_pairs(value) if _FIELD_WORD.search(value) else _pure_value_pairs(value)
        hint = _hint_words(m.group("subject"))
        return QuickEdit(pairs, hint) if pairs and hint else None
    pairs = parse_quick_edit(text, in_reply)
    return QuickEdit(pairs, []) if pairs else None


def parse_quick_delete(text: str, in_reply: bool) -> bool:
    """'hapus yang tadi' / 'hapus itu' (atau 'hapus' sebagai balasan ke pesan catatan)."""
    m = _DELETE.match(text)
    if not m:
        return False
    rest = m.group("rest")
    leftover = re.sub(r"[\s,.!]+", " ", _FILLER.sub(" ", rest)).strip()
    if leftover:  # 'hapus semua', 'hapus kopi 25rb', dst. bukan perintah hapus satu catatan
        return False
    return in_reply or bool(_TARGET_WORD.search(rest))
