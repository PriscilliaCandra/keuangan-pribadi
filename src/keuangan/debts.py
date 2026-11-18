"""Utang-piutang dan patungan: penyimpanan, pemahaman kalimat, pengingat, dan format pesan.

Dicatat TERPISAH dari pengeluaran/pemasukan: meminjamkan atau menerima pelunasan bukan belanja.
Hanya bagianmu dari patungan yang menjadi pengeluaran.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from .categories import LAINNYA, guess_category
from .db import Database
from .formatting import rupiah, tanggal
from .parser import find_amount

PIUTANG, UTANG = "piutang", "utang"        # piutang: orang berutang padaku · utang: aku berutang
MAX_NAME = 40
MAX_OPEN_DEBTS = 200
MAX_AMOUNT = 10_000_000_000
MAX_SPLIT_PEOPLE = 15
ME_WORDS = {"aku", "saya", "gue", "gua", "gw", "ane"}


def clean_person(raw: str) -> str:
    """Nama orang: tanpa simbol aneh, spasi rapi, maksimal 40 karakter; 'budi' -> 'Budi'."""
    name = re.sub(r"\s+", " ", re.sub(r"[^\w .'-]", "", raw or "")).strip(" .,'-")[:MAX_NAME].strip()
    return name.title() if name == name.lower() else name


def person_key(name: str) -> str:
    return name.strip().lower()


# ---------- penyimpanan ----------
@dataclass
class Debt:
    id: int
    chat_id: int
    person: str
    direction: str
    amount: int
    paid: int
    note: str
    created_on: date
    due_on: date | None
    last_reminded: str | None

    @property
    def outstanding(self) -> int:
        return self.amount - self.paid

    @property
    def key(self) -> str:
        return person_key(self.person)


@dataclass
class PayResult:
    person: str
    direction: str
    applied: int
    excess: int        # kelebihan dibanding sisa utang
    remaining: int     # sisa untuk orang & arah ini setelah pembayaran
    settled: int       # berapa catatan yang lunas


class DebtStore:
    _SELECT = ("SELECT d.*, COALESCE((SELECT SUM(p.amount) FROM debt_payments p WHERE p.debt_id=d.id), 0)"
               " AS paid FROM debts d")

    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _debt(row) -> Debt:
        return Debt(row["id"], row["chat_id"], row["person"], row["direction"], row["amount"], row["paid"],
                    row["note"], date.fromisoformat(row["created_on"]),
                    date.fromisoformat(row["due_on"]) if row["due_on"] else None, row["last_reminded"])

    def add(self, chat_id: int, person: str, direction: str, amount: int, note: str,
            created_on: date, due_on: date | None = None) -> Debt:
        person = clean_person(person)
        if direction not in (PIUTANG, UTANG):
            raise ValueError("arah harus piutang atau utang")
        if not person:
            raise ValueError("nama orang kosong")
        if not 0 < amount <= MAX_AMOUNT:
            raise ValueError("jumlah tidak wajar")
        if len(self.list(chat_id)) >= MAX_OPEN_DEBTS:
            raise ValueError(f"maksimal {MAX_OPEN_DEBTS} catatan utang-piutang yang masih terbuka")
        cur = self.db.execute(
            "INSERT INTO debts (chat_id, person, person_key, direction, amount, note, created_on, due_on)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (chat_id, person, person_key(person), direction, amount, note.strip()[:80],
             created_on.isoformat(), due_on.isoformat() if due_on else None))
        return self.get(chat_id, int(cur.lastrowid))

    def get(self, chat_id: int, debt_id: int) -> Debt:
        return self._debt(self.db.query(self._SELECT + " WHERE d.chat_id=? AND d.id=?", (chat_id, debt_id))[0])

    def list(self, chat_id: int, open_only: bool = True) -> list[Debt]:
        rows = self.db.query(self._SELECT + " WHERE d.chat_id=? ORDER BY d.created_on, d.id", (chat_id,))
        debts = [self._debt(r) for r in rows]
        return [d for d in debts if d.outstanding > 0] if open_only else debts

    def directions_for(self, chat_id: int, person: str) -> set[str]:
        key = person_key(person)
        return {d.direction for d in self.list(chat_id) if d.key == key}

    def match_person(self, chat_id: int, text: str) -> list[str]:
        """Nama orang (yang masih punya catatan terbuka) yang cocok dengan teks; selalu per chat_id."""
        key = person_key(clean_person(text))
        if not key:
            return []
        people = {}
        for d in self.list(chat_id):
            people.setdefault(d.key, d.person)
        if key in people:
            return [people[key]]
        return [name for k, name in people.items() if key in k or k in key]

    def pay(self, chat_id: int, person: str, amount: int | None, on_date: date,
            direction: str | None = None) -> PayResult | None:
        """Catat pembayaran (None = lunasi semua), mengisi catatan terlama dulu. None bila tak ada utang."""
        key = person_key(person)
        debts = [d for d in self.list(chat_id) if d.key == key and (direction is None or d.direction == direction)]
        if not debts:
            return None
        if len({d.direction for d in debts}) > 1:
            raise ValueError("arah ambigu")
        total_out = sum(d.outstanding for d in debts)
        left = total_out if amount is None else amount
        applied = settled = 0
        for d in debts:
            if left <= 0:
                break
            take = min(d.outstanding, left)
            self.db.execute("INSERT INTO debt_payments (debt_id, chat_id, amount, on_date) VALUES (?, ?, ?, ?)",
                            (d.id, chat_id, take, on_date.isoformat()))
            left -= take
            applied += take
            settled += 1 if take == d.outstanding else 0
        return PayResult(debts[0].person, debts[0].direction, applied, max(0, left),
                         total_out - applied, settled)

    def delete_person(self, chat_id: int, person: str, direction: str | None = None) -> int:
        key = person_key(person)
        ids = [d.id for d in self.list(chat_id, open_only=False)
               if d.key == key and (direction is None or d.direction == direction)]
        for debt_id in ids:
            self.db.execute("DELETE FROM debt_payments WHERE debt_id=? AND chat_id=?", (debt_id, chat_id))
            self.db.execute("DELETE FROM debts WHERE id=? AND chat_id=?", (debt_id, chat_id))
        return len(ids)

    def list_payments(self, chat_id: int) -> list[tuple[str, str, date, int]]:
        """(orang, arah, tanggal, jumlah) semua pembayaran milik chat_id."""
        rows = self.db.query(
            "SELECT d.person AS person, d.direction AS direction, p.on_date AS on_date, p.amount AS amount"
            " FROM debt_payments p JOIN debts d ON d.id = p.debt_id WHERE p.chat_id=? AND d.chat_id=?"
            " ORDER BY p.on_date, p.id", (chat_id, chat_id))
        return [(r["person"], r["direction"], date.fromisoformat(r["on_date"]), r["amount"])
                for r in rows]

    def totals(self, chat_id: int) -> tuple[int, int]:
        """(orang berutang padaku, aku berutang)."""
        debts = self.list(chat_id)
        return (sum(d.outstanding for d in debts if d.direction == PIUTANG),
                sum(d.outstanding for d in debts if d.direction == UTANG))

    def mark_reminded(self, chat_id: int, debt_id: int, token: str) -> None:
        self.db.execute("UPDATE debts SET last_reminded=? WHERE id=? AND chat_id=?", (token, debt_id, chat_id))


# ---------- pengingat jatuh tempo ----------
@dataclass
class DebtReminder:
    debt: Debt
    stage: str   # awal | hari-h | lewat
    token: str
    days: int    # hari menuju (negatif = terlewat)


def due_reminders(store: DebtStore, chat_id: int, today: date, days_ahead: int = 3) -> list[DebtReminder]:
    """Tiga tahap per jatuh tempo: H-3, hari-H, dan sekali setelah lewat."""
    out = []
    for d in store.list(chat_id):
        if d.due_on is None:
            continue
        left = (d.due_on - today).days
        if left > days_ahead:
            continue
        stage = "lewat" if left < 0 else "hari-h" if left == 0 else "awal"
        token = f"{d.due_on.isoformat()}:{stage}"
        if d.last_reminded != token:
            out.append(DebtReminder(d, stage, token, left))
    return sorted(out, key=lambda r: r.debt.due_on or today)


def format_reminders(items: list[DebtReminder]) -> str:
    lines = ["💸 Pengingat utang-piutang"]
    for r in items:
        d = r.debt
        when = ("HARI INI" if r.days == 0 else f"lewat {-r.days} hari" if r.days < 0
                else f"{r.days} hari lagi ({tanggal(d.due_on)})")
        note = f" ({d.note})" if d.note else ""
        if d.direction == PIUTANG:
            lines.append(f"• {d.person} belum membayar {rupiah(d.outstanding)}{note} — {when}. Draf pesan: /tagih {d.person}")
        else:
            lines.append(f"• Kamu perlu membayar {d.person} {rupiah(d.outstanding)}{note} — {when}. Setelah bayar: /lunas {d.person}")
    return "\n".join(lines)


# ---------- format ----------
def _group(debts: list[Debt], direction: str) -> list[tuple[str, int, list[str], date | None]]:
    groups: dict[str, dict] = {}
    for d in debts:
        if d.direction != direction:
            continue
        g = groups.setdefault(d.key, {"person": d.person, "sum": 0, "notes": [], "due": None})
        g["sum"] += d.outstanding
        if d.note and d.note not in g["notes"]:
            g["notes"].append(d.note)
        if d.due_on and (g["due"] is None or d.due_on < g["due"]):
            g["due"] = d.due_on
    return sorted(((g["person"], g["sum"], g["notes"], g["due"]) for g in groups.values()), key=lambda x: -x[1])


def _line(person: str, total: int, notes: list[str], due: date | None, today: date) -> str:
    extra = f" ({', '.join(notes[:2])})" if notes else ""
    if due:
        flag = " ⚠️ lewat tempo" if due < today else f" · tempo {tanggal(due)}"
        extra += flag
    return f"• {person} — {rupiah(total)}{extra}"


def format_ledger(debts: list[Debt], today: date) -> str:
    owed_to_me, owed_by_me = _group(debts, PIUTANG), _group(debts, UTANG)
    if not owed_to_me and not owed_by_me:
        return ("Tidak ada utang-piutang yang terbuka. 🎉\n"
                "Catat: \"Budi utang 50rb makan siang\" · \"utang ke Siti 30rb\" · /patungan makan 240rb dengan Budi, Siti")
    lines = ["💸 Utang & piutang"]
    if owed_to_me:
        lines.append(f"\nOrang yang berutang padamu — total {rupiah(sum(g[1] for g in owed_to_me))}")
        lines += [_line(*g, today) for g in owed_to_me]
    if owed_by_me:
        lines.append(f"\nKamu berutang — total {rupiah(sum(g[1] for g in owed_by_me))}")
        lines += [_line(*g, today) for g in owed_by_me]
    lines.append("\nBayar/terima: /lunas <nama> [jumlah] · Draf tagihan: /tagih <nama>")
    return "\n".join(lines)


def format_tagih(person: str, debts: list[Debt]) -> str:
    total = sum(d.outstanding for d in debts)
    notes = list(dict.fromkeys(d.note for d in debts if d.note))
    for_what = f" untuk {', '.join(notes[:3])}" if notes else ""
    return (f"Halo {person} 👋 mau mengingatkan ya, masih ada {rupiah(total)}{for_what} yang belum masuk. "
            "Kalau sudah sempat, boleh ditransfer. Terima kasih banyak! 🙏")


# ---------- pemahaman kalimat ----------
STOP = {"buat", "untuk", "utk", "karena", "tempo", "jatuh", "rp", "rb", "ribu", "jt", "juta", "k", "tgl",
        "yang", "udah", "sudah", "lunas", "sebesar", "seceng", "goceng", "ceban", "noban", "gocap", "cepek",
        "seribu", "sejuta", "dan", "sebanyak"}
_PREP = r"(?:ke|sama|dari|pada|kepada|oleh)"
_DUE = re.compile(
    r"\b(?:jatuh\s+tempo|tempo)\s+(\d{4}-\d{1,2}(?:-\d{1,2})?|\d{1,3}\s*hari\s*lagi|besok|minggu\s+depan|bulan\s+depan)",
    re.I)


@dataclass
class DebtIntent:
    kind: str                 # piutang | utang | terima | bayar | lunas
    name: str
    amount: int | None
    note: str = ""
    due: date | None = None
    error: str | None = None


def _parse_due_token(raw: str) -> date | None:
    """2026-12-20 atau 2026-12 (akhir bulan). Tanggal lampau BOLEH: utang yang sudah lewat tempo tetap dicatat."""
    try:
        if m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw):
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if m := re.fullmatch(r"(\d{4})-(\d{1,2})", raw):
            first = date(int(m.group(1)), int(m.group(2)), 1)
            return (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    except ValueError:
        return None
    return None


def _extract_due(text: str, today: date) -> tuple[date | None, str, bool]:
    m = _DUE.search(text)
    if not m:
        # kata "tempo" ada tapi isinya tak dikenali: jangan diam-diam masuk ke catatan
        return None, text, bool(re.search(r"(?i)\b(?:jatuh\s+)?tempo\b", text))
    raw = m.group(1).lower().strip()
    due: date | None = None
    if raw == "besok":
        due = today + timedelta(days=1)
    elif raw.startswith("minggu"):
        due = today + timedelta(days=7)
    elif raw.startswith("bulan"):
        due = today + timedelta(days=30)
    elif re.fullmatch(r"\d{1,3}\s*hari\s*lagi", raw):
        due = today + timedelta(days=int(re.match(r"\d+", raw).group()))
    else:
        due = _parse_due_token(raw)
    return due, text[: m.start()] + " " + text[m.end():], due is None


def _take_name(text: str) -> tuple[str, str]:
    """Ambil 1-2 kata nama dari awal teks (berhenti di angka/kata sambung). -> (nama, sisa)."""
    tokens = text.split()
    taken: list[str] = []
    while tokens and len(taken) < 2:
        tok = tokens[0]
        if re.match(r"^\d", tok) or tok.lower() in STOP or tok.lower() in ME_WORDS:
            break
        taken.append(tokens.pop(0))
    return clean_person(" ".join(taken)), " ".join(tokens)


def _clean_note(text: str) -> str:
    note = re.sub(r"\s+", " ", text).strip(" .,:;-")
    note = re.sub(r"(?i)^(?:buat|untuk|utk|karena|dari|soal|yang)\s+", "", note).strip()
    return note[:80]


def _amount_and_rest(body: str) -> tuple[int | None, str]:
    found = find_amount(body)
    if not found:
        return None, body
    s, e = found[1]
    return found[0], body[:s] + " " + body[e:]


_PAY_VERBS = r"(?:bayar|nyicil|cicil|lunas|transfer|ngembaliin|mengembalikan|balikin|bayarin)"
_RE_TERIMA = re.compile(
    rf"^(?P<name>\S+(?:\s+\S+){{0,2}}?)\s+(?:udah\s+|sudah\s+|lagi\s+|baru\s+)?{_PAY_VERBS}\b(?P<rest>.*)$", re.I)
_RE_BAYAR = re.compile(
    rf"^(?:(?:aku|saya|gue|gua|gw)\s+)?(?:udah\s+|sudah\s+)?(?:bayar|nyicil|cicil|lunasin|lunasi|balikin|kembalikan|ngembaliin)"
    rf"\s+(?:utang\s+|hutang\s+)?{_PREP}\s+(?P<rest>.+)$", re.I)
_RE_LUNAS = re.compile(r"^lunas(?:in|i)?\s+(?P<rest>.+)$", re.I)
_RE_PIUTANG_A = re.compile(
    r"^(?:pinjamin|minjemin|pinjamkan|meminjamkan|ngutangin|utangin|talangin|menalangi)\s+(?P<body>.+)$", re.I)
_RE_UTANG = re.compile(
    rf"^(?:(?:aku|saya|gue|gua|gw)\s+)?(?:ngutang|utang|hutang|berutang|pinjam|minjem|meminjam)\s+{_PREP}\s+(?P<body>.+)$",
    re.I)
_RE_UTANG_B = re.compile(
    rf"^(?:(?:aku|saya|gue|gua|gw)\s+)?(?:ngutang|utang|hutang|berutang|pinjam|minjem|meminjam)\s+"
    rf"(?P<before>.+?)\s+{_PREP}\s+(?P<after>.+)$", re.I)
_RE_PIUTANG_B = re.compile(
    r"^(?P<name>\S+(?:\s+\S+){0,2}?)\s+(?:ngutang|utang|hutang|berutang|minjem|pinjam|meminjam)\s+(?P<body>.+)$", re.I)


def parse_debt_text(text: str, today: date) -> DebtIntent | None:
    """Kalimat utang-piutang -> maksud. None bila bukan. Pembayaran ('Budi bayar 20rb') baru
    dianggap sah oleh pemanggil bila orangnya memang punya utang terbuka."""
    t = re.sub(r"\s+", " ", text).strip()
    if not t:
        return None
    due, t, bad_due = _extract_due(t, today)
    intent = _parse_core(t, due)
    if intent is not None and bad_due:   # galat tempo hanya untuk kalimat yang memang utang-piutang
        return DebtIntent(intent.kind, "", None, error="Tempo tidak terbaca. Contoh: tempo 2026-12-20, "
                                                     "tempo besok, tempo 7 hari lagi")
    return intent


def _parse_core(t: str, due: date | None) -> DebtIntent | None:
    if m := _RE_BAYAR.match(t):                      # "bayar utang ke Budi 50rb"
        name, rest = _take_name(m.group("rest"))
        amount, _ = _amount_and_rest(rest)
        return DebtIntent("bayar", name, amount) if name else None
    if m := _RE_LUNAS.match(t):                      # "lunas Budi"
        name, rest = _take_name(m.group("rest"))
        amount, _ = _amount_and_rest(rest)
        return DebtIntent("lunas", name, amount) if name else None
    if (m := _RE_TERIMA.match(t)) and person_key(m.group("name")) not in ME_WORDS \
            and not re.match(rf"(?i)^{_PAY_VERBS}\b", t):    # "Budi bayar 20rb"
        name = clean_person(m.group("name"))
        amount, _ = _amount_and_rest(m.group("rest"))
        return DebtIntent("terima", name, amount) if name else None
    if m := _RE_PIUTANG_A.match(t):                  # "pinjamin Budi 100rb"
        name, rest = _take_name(m.group("body"))
        amount, rest2 = _amount_and_rest(m.group("body")[len(name):] if name else rest)
        if not name or amount is None:
            return None
        return DebtIntent(PIUTANG, name, amount, _clean_note(_strip_name(rest2, name)), due)
    if m := _RE_UTANG.match(t):                      # "utang ke Budi 50rb buat bensin"
        name, rest = _take_name(m.group("body"))
        amount, rest2 = _amount_and_rest(rest)
        if not name or amount is None:
            return None
        return DebtIntent(UTANG, name, amount, _clean_note(rest2), due)
    if m := _RE_UTANG_B.match(t):                    # "pinjam 50rb dari Budi buat bensin"
        amount, note_before = _amount_and_rest(m.group("before"))
        name, rest = _take_name(m.group("after"))
        if not name or amount is None:
            return None
        return DebtIntent(UTANG, name, amount, _clean_note(f"{note_before} {rest}"), due)
    if (m := _RE_PIUTANG_B.match(t)) and person_key(m.group("name")) not in ME_WORDS \
            and not re.match(rf"(?i)^{_PREP}\b", m.group("body")):   # "Budi utang 50rb makan siang"
        name = clean_person(m.group("name"))
        amount, rest = _amount_and_rest(m.group("body"))
        if not name or amount is None or name.lower().split()[0] in {"bayar", "cicil", "lunas", "tabung", "nabung"}:
            return None
        return DebtIntent(PIUTANG, name, amount, _clean_note(rest), due)
    return None


def _strip_name(text: str, name: str) -> str:
    return re.sub(re.escape(name), " ", text, count=1, flags=re.I)


# ---------- patungan ----------
@dataclass
class SplitPlan:
    description: str
    total: int
    people: int                              # jumlah orang termasuk aku
    my_share: int
    payer: str                               # "aku" atau nama
    category: str
    debts: list[tuple[str, int, str]] = field(default_factory=list)   # (nama, jumlah, arah)


_RE_SPLIT = re.compile(r"^(?:patungan|urunan|split)\b[:\s]*(?P<body>.*)$", re.I)
_RE_WITH = re.compile(r"\b(?:dengan|bareng|bersama|sama)\s+", re.I)
_RE_PAYER = re.compile(r"\b(?:dibayar(?:in)?|ditalangin|ditalangi|yang\s+bayar)\s+(?P<name>[^\d,]+?)(?=\s+(?:dengan|bareng|bagi)\b|[,.]|$)", re.I)
_RE_SHARES = re.compile(r"\bbagi\s+(?P<rest>.+)$", re.I)
_RE_N_PEOPLE = re.compile(r"\b(?:(\d{1,2})\s*orang|(bertiga|berempat|berlima|berenam|berdua))\b", re.I)
_N_WORDS = {"berdua": 2, "bertiga": 3, "berempat": 4, "berlima": 5, "berenam": 6}


def _split_names(text: str) -> list[str]:
    parts = re.split(r"\s*(?:,|&|\+|\bdan\b)\s*", text, flags=re.I)
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        name = clean_person(part)
        k = person_key(name)
        if name and k not in ME_WORDS and k not in seen:
            seen.add(k)
            out.append(name)
    return out


def parse_split(text: str, today: date) -> SplitPlan | str | None:
    """'patungan makan malam 240rb dengan Budi, Siti, Ani' -> rencana. str = pesan galat, None = bukan patungan."""
    m = _RE_SPLIT.match(text.strip())
    if not m:
        return None
    body = m.group("body")
    usage = ("Contoh: patungan makan malam 240rb dengan Budi, Siti, Ani\n"
             "Dibayar teman: patungan bensin 150rb dengan Budi dibayar Budi\n"
             "Bagian berbeda: patungan hadiah 300rb bagi Budi 120rb, Siti 80rb\n"
             "Tanpa nama: patungan makan 200rb 4 orang")

    shares_text = None
    if sm := _RE_SHARES.search(body):
        shares_text, body = sm.group("rest"), body[: sm.start()]
    payer = "aku"
    if pm := _RE_PAYER.search(body):
        payer_name = clean_person(pm.group("name"))
        if payer_name and person_key(payer_name) not in ME_WORDS:
            payer = payer_name
        body = body[: pm.start()] + " " + body[pm.end():]
    names: list[str] = []
    lead = body
    if wm := _RE_WITH.search(body):
        lead, names_text = body[: wm.start()], body[wm.end():]
        names = _split_names(re.sub(_RE_N_PEOPLE, " ", names_text))
    anon = None
    if nm := _RE_N_PEOPLE.search(body):
        anon = int(nm.group(1)) if nm.group(1) else _N_WORDS[nm.group(2).lower()]
        lead = lead.replace(nm.group(0), " ")

    found = find_amount(lead)
    if not found or found[0] > MAX_AMOUNT:
        return "Total patungan tidak terbaca.\n" + usage
    total = found[0]
    s, e = found[1]
    description = _clean_note(lead[:s] + " " + lead[e:]) or "Patungan"
    category = guess_category(description) or LAINNYA

    others: list[tuple[str, int | None]] = []     # (nama, jumlah atau None bila dibagi rata)
    my_explicit: int | None = None
    if shares_text is not None:
        for seg in re.split(r"\s*[,&+]\s*|\s+\bdan\b\s+", shares_text, flags=re.I):
            amt = find_amount(seg)
            if not amt:
                return f"Bagian '{seg.strip()}' tidak terbaca. Format: bagi Budi 120rb, Siti 80rb\n" + usage
            name = clean_person(seg[: amt[1][0]])
            if not name:
                return "Ada bagian tanpa nama. Format: bagi Budi 120rb, Siti 80rb\n" + usage
            if person_key(name) in ME_WORDS:
                my_explicit = amt[0]
            else:
                others.append((name, amt[0]))
    else:
        others = [(n, None) for n in names]
    if payer != "aku" and person_key(payer) not in {person_key(n) for n, _ in others}:
        others.append((payer, None))
    if len({person_key(n) for n, _ in others}) != len(others):
        return "Ada nama yang kembar di daftar patungan."
    if len(others) + 1 > MAX_SPLIT_PEOPLE:
        return f"Maksimal {MAX_SPLIT_PEOPLE} orang per patungan."

    plan = SplitPlan(description, total, len(others) + 1, 0, payer, category)
    if shares_text is not None:                                    # bagian ditentukan manual
        given = sum(a or 0 for _, a in others)
        if my_explicit is not None:
            if given + my_explicit != total:
                return (f"Jumlah semua bagian ({rupiah(given + my_explicit)}) tidak sama dengan total "
                        f"({rupiah(total)}).")
            plan.my_share = my_explicit
        else:
            if given > total:
                return f"Jumlah bagian teman ({rupiah(given)}) melebihi total ({rupiah(total)})."
            plan.my_share = total - given
        amounts = {n: a for n, a in others}
    else:                                                          # dibagi rata
        count = len(others) + 1 if others else (anon or 1)
        if count < 2:
            return "Patungan perlu minimal 2 orang. Tambahkan nama (dengan Budi) atau jumlah (4 orang).\n" + usage
        plan.people = count
        base = total // count
        remainder = total - base * count                           # sisa pembulatan ditanggung pembayar
        amounts = {n: base for n, _ in others}
        if payer == "aku":
            plan.my_share = base + remainder
        else:
            amounts[payer] = base + remainder
            plan.my_share = base
    if payer == "aku":
        plan.debts = [(n, amounts[n], PIUTANG) for n, _ in others if amounts[n] > 0]
    elif plan.my_share > 0:
        plan.debts = [(payer, plan.my_share, UTANG)]               # aku berutang bagianku ke pembayar
    return plan


def format_split_preview(plan: SplitPlan) -> str:
    lines = [f"🍽️ Patungan: {plan.description} — total {rupiah(plan.total)}, {plan.people} orang",
             f"Bagianmu: {rupiah(plan.my_share)} → dicatat sebagai pengeluaran ({plan.category})"]
    if plan.payer == "aku":
        if plan.debts:
            lines.append("Kamu yang membayar, jadi ditagih ke:")
            lines += [f"• {n} {rupiah(a)}" for n, a, _ in plan.debts]
        else:
            lines.append("Kamu yang membayar; teman tidak dicatat (tanpa nama).")
    else:
        lines.append(f"{plan.payer} yang membayar, jadi kamu berutang {rupiah(plan.my_share)} ke {plan.payer}.")
    lines.append('\nSimpan? Balas "ya" atau "batal".')
    return "\n".join(lines)
