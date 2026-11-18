"""Logika aplikasi, bebas dari Telegram. Adapter kanal (Telegram, WhatsApp, ...) cukup memanggil
metode di sini dan mengirim string hasilnya."""
from __future__ import annotations

import imaplib
import logging
import re
import threading
import time
from datetime import date, timedelta
from typing import Any, Callable

from . import bills as bills_mod
from . import agent, budget, email_scan, ledger, reports
from . import debts as debts_mod
from . import plans as plans_mod
from .access import AccessControl, AiBusy, QuotaExceeded
from .categories import CATEGORIES, LAINNYA, guess_category, match_category
from .db import Database
from .formatting import rupiah, tanggal
from .llm import Llm, LlmError, to_int
from .parser import (ParsedExpense, ParsedIncome, find_amount, is_question, parse_expense,
                     parse_income, parse_quick_delete, parse_quick_edit_full)

log = logging.getLogger(__name__)
_REF_IN_TEXT = re.compile(r"\(ID (m?\d+)\)")

HELP = """Halo! Aku pencatat keuanganmu.

✍️ Catat pengeluaran: "makan siang 35rb", "bensin 50k kemarin", atau kirim foto struk.
💵 Catat pemasukan: "gaji 8jt", "+500rb freelance", atau /masuk bonus 1jt
💼 /saldo — pemasukan, pengeluaran, selisih bulan ini
🕘 /riwayat — catatan terakhir + ID
✏️ Koreksi: balas (reply) pesan "Dicatat…" dengan "transportasi", "40rb", atau "kemarin".
   Tanpa reply: "ubah jadi 40rb", "kategorinya transport" (untuk catatan terakhir).
   Atau /edit 12 kategori transport · /edit 12 nominal 40rb
🗑️ "hapus yang tadi" · /hapus — catatan terakhir · /hapus 12 — per ID
📤 /ekspor — CSV bulan ini · /ekspor semua · /ekspor 2026-10
🤖 /tanya berapa pengeluaran kopi bulan ini? (atau ketik pertanyaannya langsung)
📈 /dashboard — grafik pemasukan & pengeluaran (tautan masuk sekali pakai)

🧮 /aman — uang aman per hari sampai gajian (atur tanggal gajian: /gajian 25)
🎯 /target liburan bali 5jt sampai 2027-03 · "nabung 500rb liburan" · /tarik liburan 200rb
💸 Utang-piutang: "Budi utang 50rb makan siang" · "utang ke Siti 30rb" · "Budi bayar 20rb" · /utang · /lunas Budi · /tagih Budi
🍽️ Patungan: "patungan makan malam 240rb dengan Budi, Siti, Ani" (ada pratinjau, balas "ya")
💡 Nominal bisa ditulis sehari-hari: goceng, ceban, gocap, cepek, sejuta

💰 /anggaran — lihat anggaran
   /anggaran makanan 1,5jt — atur anggaran per kategori
   /anggaran total 5jt — anggaran total bulanan
   /anggaran hapus makanan

📊 /ringkasan — 7 hari terakhir · /ringkasan bulan

🧾 /tagihan — daftar tagihan & langganan
   /tagihan listrik 350rb tgl 20
   /langganan netflix 54rb tgl 5
   /langganan pajak motor 2jt tgl 10 bulan 3 tahunan
   /pakai netflix — konfirmasi masih dipakai
   /batal netflix — tandai sudah berhenti
📧 /scan_email — cek email tagihan sekarang

Kategori: """ + ", ".join(CATEGORIES)

NO_AMOUNT = 'Aku tidak menemukan nominalnya. Coba: "makan siang 35rb" atau "kopi 1,5k".'
AI_BUSY = "AI sedang sibuk melayani pengguna lain. Coba lagi sebentar. Pencatatan teks tetap berjalan."


def _quota_message(exc: QuotaExceeded) -> str:
    return (f"Kuota AI harianmu habis ({exc.used}/{exc.limit}); kembali normal besok. "
            "Pencatatan teks, anggaran, dan dashboard tetap berjalan.")


def receipt_to_expense(data: dict[str, Any] | None, today: date) -> ParsedExpense | None:
    """Validasi keluaran model untuk foto struk sebelum dipercaya."""
    if not data or data.get("is_receipt") is not True:
        return None
    total = to_int(data.get("total"))
    if total is None or total > 1_000_000_000:
        return None
    try:
        spent = date.fromisoformat(str(data.get("date")))
    except ValueError:
        spent = today
    if spent > today + timedelta(days=1) or spent < today - timedelta(days=366):
        spent = today
    merchant = str(data.get("merchant") or "Struk").strip()[:80] or "Struk"
    category = data.get("category")
    if category not in CATEGORIES:
        category = guess_category(merchant) or LAINNYA
    return ParsedExpense(int(total), merchant, category, spent)


class Service:
    def __init__(self, db: Database, llm: Llm | None = None,
                 clock: Callable[[], date] = date.today, reminder_days: int = 3,
                 email_fetcher: Callable[[], list[email_scan.RawEmail]] | None = None,
                 access: AccessControl | None = None):
        self.db = db
        self.llm = llm
        self.clock = clock
        self.reminder_days = reminder_days
        self.email_fetcher = email_fetcher
        # Tanpa konfigurasi: tak ada admin dan kuota AI longgar (dipakai tes & mode sederhana).
        self.access = access or AccessControl(db, ai_daily_limit=100_000, clock=clock)
        self.plans = plans_mod.PlanStore(db)
        self.debts = debts_mod.DebtStore(db)
        self._pending_splits: dict[int, tuple[debts_mod.SplitPlan, float]] = {}  # menunggu "ya"/"batal"
        self._pending_lock = threading.Lock()
        self._scan_lock = threading.Lock()  # satu pemindaian email pada satu waktu
        self.dashboard_base_url: str | None = None  # diisi saat dashboard menyala

    def help_text(self, chat_id: int) -> str:
        return HELP + self.access.help_extra(chat_id)

    def dashboard_command(self, chat_id: int, args: str = "") -> str:
        """Tautan masuk dashboard: token sekali pakai, berlaku 10 menit, khusus pemintanya."""
        if not self.dashboard_base_url:
            return "Dashboard tidak aktif (cek DASHBOARD_PORT di .env, 0 berarti dimatikan)."
        token = self.access.accounts.create_login_token(chat_id, ttl_seconds=600)
        return ("📈 Dashboard-mu:\n"
                f"{self.dashboard_base_url}/?t={token}\n"
                "Tautan ini sekali pakai dan berlaku 10 menit. Jangan dibagikan. "
                "Kirim /dashboard lagi bila kedaluwarsa.")

    # ---------- pengeluaran ----------
    def handle_text(self, chat_id: int, text: str, reply_to: str | None = None) -> str:
        """Urutan: hapus/koreksi di chat -> pertanyaan (jika ada AI) -> pemasukan -> pengeluaran.

        `reply_to`: teks pesan bot yang sedang dibalas user. ID catatan diambil dari "(ID 12)"
        di pesan itu, jadi balasan "transportasi" mengubah catatan yang dibalas.
        """
        today = self.clock()
        answered = self._answer_pending_split(chat_id, text)   # "ya"/"batal" untuk patungan yang menunggu
        if answered is not None:
            return answered
        ref = _REF_IN_TEXT.search(reply_to or "")
        target = ref.group(1) if ref else None
        if parse_quick_delete(text, target is not None):
            token = target or self._latest_ref(chat_id)
            return (ledger.delete_entry(self.db, chat_id, token) if token
                    else "Belum ada catatan untuk dihapus.")
        quick = parse_quick_edit_full(text, target is not None)
        if quick:
            note = ""
            if target:
                token = target
            elif quick.hint:
                token = self._find_ref(chat_id, quick.hint)
                if token is None:
                    return (f"Aku tidak menemukan catatan yang mengandung '{' '.join(quick.hint)}'. "
                            "Balas (reply) pesan catatannya, atau lihat /riwayat.")
                note = f"\n(Dipilih catatan terbaru yang cocok dengan '{' '.join(quick.hint)}'.)"
            else:
                token = self._latest_ref(chat_id)
                note = "\n(Ini catatan terakhir. Untuk catatan lain, balas pesan catatannya.)"
            if token is None:
                return "Belum ada catatan untuk diubah."
            reply = ledger.apply_edits(self.db, chat_id, token, quick.pairs, today)
            return reply + (note if reply.startswith("✏️") else "")
        saved = self._saving_from_text(chat_id, text)
        if saved is not None:
            return saved
        for handler in (self._split_from_text, self._debt_from_text):
            handled = handler(chat_id, text)
            if handled is not None:
                return handled
        if self.llm and is_question(text):
            return self.ask_command(chat_id, text)
        income = parse_income(text, today)
        if income is not None:
            return self.save_income(chat_id, income, "teks")
        parsed = parse_expense(text, today)
        if parsed is None:
            return NO_AMOUNT
        if parsed.category is None:
            parsed.category = self._llm_category(chat_id, parsed.description) or LAINNYA
        return self.save_expense(chat_id, parsed, "teks")

    def _find_ref(self, chat_id: int, words: list[str]) -> str | None:
        """Catatan terbaru (dari 200 terakhir) yang deskripsinya mengandung salah satu kata."""
        for kind, entry in self.db.recent_entries(chat_id, 200):
            description = entry.description.lower()
            if any(w in description for w in words):
                return ledger.ref_token(kind, entry)
        return None

    def _latest_ref(self, chat_id: int) -> str | None:
        recent = self.db.recent_entries(chat_id, 1)
        return ledger.ref_token(*recent[0]) if recent else None

    def _llm_category(self, chat_id: int, description: str) -> str | None:
        if not self.llm:
            return None
        try:
            with self.access.ai_slot(chat_id, count=False):  # murah: tak makan kuota, tetap antre
                return self.llm.categorize(description)
        except (LlmError, AiBusy) as exc:
            log.warning("Kategorisasi AI dilewati: %s", exc)
            return None  # jatuh ke "Lainnya"; pencatatan tetap berhasil

    def save_expense(self, chat_id: int, parsed: ParsedExpense, source: str) -> str:
        today = self.clock()
        category = parsed.category or LAINNYA
        entry_id = self.db.add_expense(chat_id, parsed.amount, parsed.description, category,
                                       parsed.spent_on, source)
        lines = [f"✅ Dicatat: {rupiah(parsed.amount)} · {category} (ID {entry_id})",
                 f"📝 {parsed.description}"]
        if parsed.spent_on != today:
            lines.append(f"📆 {tanggal(parsed.spent_on)}")
        start, end = budget.month_bounds(parsed.spent_on)
        lines.append(f"Bulan ini di {category}: {rupiah(self.db.sum_between(chat_id, start, end, category))}")
        # Peringatan hanya relevan bila pengeluaran jatuh di bulan berjalan.
        if parsed.spent_on.strftime("%Y-%m") == today.strftime("%Y-%m"):
            lines.extend(budget.check_alerts(self.db, chat_id, category, today))
        return "\n".join(lines)

    def read_receipt(self, chat_id: int, image: bytes,
                     media_type: str = "image/jpeg") -> tuple[ParsedExpense | None, str]:
        """Baca foto struk. Mengembalikan (pengeluaran | None, pesan_untuk_user)."""
        if not self.llm:
            return None, ("Baca foto struk butuh AI (isi OLLAMA_MODEL atau ANTHROPIC_API_KEY di .env). "
                          "Sementara, ketik manual: \"indomaret 85rb\".")
        try:
            with self.access.ai_slot(chat_id):
                data = self.llm.read_receipt(image, media_type)
        except QuotaExceeded as exc:
            return None, _quota_message(exc)
        except AiBusy:
            return None, AI_BUSY
        except LlmError as exc:
            log.warning("Baca struk gagal: %s", exc)
            return None, "Maaf, struknya belum bisa dibaca sekarang. Coba lagi atau ketik manual."
        parsed = receipt_to_expense(data, self.clock())
        if parsed is None:
            return None, "Aku tidak yakin ini struk atau totalnya tidak terbaca. Ketik manual saja, ya."
        return parsed, (f"🧾 Struk terbaca:\n{parsed.description} — {rupiah(parsed.amount)}\n"
                        f"{parsed.category} · {tanggal(parsed.spent_on)}\nSimpan?")

    # ---------- pemasukan, riwayat, ubah, hapus, ekspor ----------
    def save_income(self, chat_id: int, parsed: ParsedIncome, source: str) -> str:
        today = self.clock()
        entry_id = self.db.add_income(chat_id, parsed.amount, parsed.description,
                                      parsed.received_on, source, parsed.category)
        start, end = budget.month_bounds(parsed.received_on)
        income = self.db.sum_income_between(chat_id, start, end)
        spent = self.db.sum_between(chat_id, start, end)
        lines = [f"💵 Pemasukan dicatat: {rupiah(parsed.amount)} · {parsed.category} (ID m{entry_id})",
                 f"📝 {parsed.description}"]
        if parsed.received_on != today:
            lines.append(f"📆 {tanggal(parsed.received_on)}")
        lines.append(f"Selisih bulan itu: {rupiah(income - spent)} "
                     f"(masuk {rupiah(income)} − keluar {rupiah(spent)})")
        return "\n".join(lines)

    def income_command(self, chat_id: int, args: str) -> str:
        parsed = parse_income(args, self.clock(), force=True)
        if parsed is None:
            return 'Format: /masuk gaji 8jt  atau cukup ketik "gaji 8jt" / "+500rb freelance".'
        return self.save_income(chat_id, parsed, "teks")

    def saldo_command(self, chat_id: int, args: str = "") -> str:
        start, end = budget.month_bounds(self.clock())
        return ledger.cashflow(self.db, chat_id, start, end, "Arus kas bulan ini")

    def history_command(self, chat_id: int, args: str = "") -> str:
        return ledger.history(self.db, chat_id)

    def edit_command(self, chat_id: int, args: str) -> str:
        return ledger.edit_entry(self.db, chat_id, args, self.clock())

    def delete_command(self, chat_id: int, args: str = "") -> str:
        return ledger.delete_entry(self.db, chat_id, args)

    def export_command(self, chat_id: int, args: str = "") -> tuple[str, bytes] | str:
        """Mengembalikan (nama_file, isi_csv), atau pesan galat (str)."""
        today = self.clock()
        word = args.strip().lower()
        if word in ("", "bulan", "bulan ini"):
            start, end = budget.month_bounds(today)
            label = today.strftime("%Y-%m")
        elif word == "semua":
            start, end, label = date(2000, 1, 1), today + timedelta(days=1), "semua"
        elif re.fullmatch(r"\d{4}-\d{2}", word):
            try:
                start, end = budget.month_bounds(date.fromisoformat(word + "-01"))
            except ValueError:
                return "Bulan tidak valid. Contoh: /ekspor 2026-10"
            label = word
        else:
            return "Format: /ekspor (bulan ini) · /ekspor semua · /ekspor 2026-10"
        return f"keuangan-{label}.csv", ledger.export_csv(self.db, chat_id, start, end)

    # ---------- tanya (agen) ----------
    def ask_command(self, chat_id: int, question: str) -> str:
        q = question.strip()
        if not q:
            return "Contoh: /tanya berapa pengeluaran kopi bulan ini?"
        if not self.llm:
            return "Fitur tanya butuh AI. Isi OLLAMA_MODEL atau ANTHROPIC_API_KEY di .env."
        today = self.clock()
        tools = agent.QueryTools(self.db, chat_id, today)
        try:
            with self.access.ai_slot(chat_id):
                answer = self.llm.chat_with_tools(agent.system_prompt(today), q[:500],
                                                  agent.TOOL_SPECS, tools.run)
        except QuotaExceeded as exc:
            return _quota_message(exc)
        except AiBusy:
            return AI_BUSY
        except LlmError as exc:
            log.warning("Tanya gagal: %s", exc)
            return "Maaf, aku belum bisa menjawab sekarang. Coba lagi sebentar lagi."
        return answer[:3500]

    # ---------- anggaran ----------
    @staticmethod
    def _budget_key(word: str) -> str | None:
        w = word.strip().lower()
        if w in {"total", "semua", "keseluruhan"}:
            return budget.TOTAL
        return match_category(w)

    def budget_command(self, chat_id: int, args: str) -> str:
        args = args.strip()
        today = self.clock()
        if not args:
            text = budget.format_statuses(budget.statuses(self.db, chat_id, today))
            forecasts = plans_mod.budget_forecasts(self.db, chat_id, today)
            return text + ("\n\n" + plans_mod.format_forecasts(forecasts) if forecasts else "")
        m = re.match(r"(?i)hapus\s+(.+)$", args)
        if m:
            key = self._budget_key(m.group(1))
            if key is None:
                return "Kategori tidak dikenal. Pilihan: " + ", ".join(CATEGORIES)
            ok = self.db.delete_budget(chat_id, key)
            return f"Anggaran {budget.label(key)} dihapus." if ok else "Anggaran itu memang belum ada."
        found = find_amount(args)
        if not found:
            return "Format: /anggaran makanan 1,5jt  atau  /anggaran total 5jt"
        amount, (s, e) = found
        key = self._budget_key((args[:s] + " " + args[e:]).strip())
        if key is None:
            return "Kategori tidak dikenal. Pilihan: total, " + ", ".join(CATEGORIES)
        self.db.set_budget(chat_id, key, amount)
        alerts = budget.check_alerts(self.db, chat_id, key if key != budget.TOTAL else LAINNYA, today)
        text = f"✅ Anggaran {budget.label(key)} per bulan: {rupiah(amount)}"
        return "\n".join([text, *alerts])

    # ---------- laporan ----------
    def summary_command(self, chat_id: int, period: str = "minggu") -> str:
        today = self.clock()
        if period.strip().lower().startswith("bul"):
            start, end = budget.month_bounds(today)
            title = "bulan ini"
        else:
            start, end = today - timedelta(days=6), today + timedelta(days=1)
            title = "7 hari"
        parts = [reports.summary(self.db, chat_id, start, end, f"Ringkasan {title}")]
        if self.db.sum_income_between(chat_id, start, end) > 0:  # hanya bila ada pemasukan
            parts.append(ledger.cashflow(self.db, chat_id, start, end, f"Arus kas {title}"))
        items = budget.statuses(self.db, chat_id, today)
        if items:
            parts.append(budget.format_statuses(items))
        forecasts = plans_mod.budget_forecasts(self.db, chat_id, today)
        if forecasts:
            parts.append(plans_mod.format_forecasts(forecasts))
        safe = plans_mod.safe_daily(self.db, self.plans, chat_id, today)
        if safe is not None and safe.available > 0:
            parts.append(f"🧮 Uang aman: {rupiah(safe.per_day)}/hari sampai {tanggal(safe.next_payday)} (detail: /aman)")
        return "\n\n".join(parts)

    def weekly_job(self, chat_id: int) -> str:
        return self.summary_command(chat_id, "minggu")

    # ---------- perencanaan: gajian, uang aman, target tabungan ----------
    MAX_GOALS = 20

    def gajian_command(self, chat_id: int, args: str = "") -> str:
        token = args.strip()
        if not token:
            day = self.plans.get_payday(chat_id)
            return (f"Tanggal gajianmu: {day}." if day else "Tanggal gajian belum diatur.") + "\nAtur: /gajian 25"
        if not token.isdigit() or not 1 <= int(token) <= 31:
            return "Format: /gajian 25 (tanggal 1-31)"
        self.plans.set_payday(chat_id, int(token))
        return f"✅ Tanggal gajian: {int(token)}. Lihat uang aman harianmu dengan /aman"

    def aman_command(self, chat_id: int, args: str = "") -> str:
        return plans_mod.format_safe_daily(plans_mod.safe_daily(self.db, self.plans, chat_id, self.clock()))

    def target_command(self, chat_id: int, args: str = "") -> str:
        today, text = self.clock(), args.strip()
        if not text:
            return plans_mod.format_goals(self.plans.list_goals(chat_id), today)
        if m := re.match(r"(?i)hapus\s+(.+)$", text):
            goal = self.plans.find_goal(chat_id, m.group(1))
            if goal is None:
                return "Target itu tidak ditemukan. Lihat daftar: /target"
            self.plans.delete_goal(chat_id, goal.id)
            return f"🗑️ Target '{goal.name}' dihapus (tabungan di dalamnya ikut dihapus dari catatan)."
        deadline = None
        if m := re.search(r"(?i)\bsampai\s+(\S+)", text):
            deadline = plans_mod.parse_deadline(m.group(1), today)
            if deadline is None:
                return "Tenggat tidak terbaca atau sudah lewat. Contoh: sampai 2027-06"
            text = text[: m.start()] + " " + text[m.end():]
        found = find_amount(text)
        name = ""
        if found:
            s, e = found[1]
            name = re.sub(r"\s+", " ", text[:s] + " " + text[e:]).strip(" -:,.")[:60]
        if not found or not name:
            return ("Format: /target <nama> <jumlah> [sampai 2027-06]\n"
                    "Contoh: /target liburan bali 5jt sampai 2027-03")
        existing = self.plans.find_goal(chat_id, name)
        if existing is None and len(self.plans.list_goals(chat_id)) >= self.MAX_GOALS:
            return f"Maksimal {self.MAX_GOALS} target. Hapus yang sudah tidak dipakai: /target hapus <nama>"
        goal, created = self.plans.create_goal(chat_id, existing.name if existing else name,
                                               found[0], deadline, today)
        head = "🎯 Target dibuat" if created else "✏️ Target diperbarui"
        return head + "\n" + plans_mod.format_goal(goal, today) + "\nTabung: /tabung " + goal.name.split()[0] + " 500rb"

    def tabung_command(self, chat_id: int, args: str = "", sign: int = 1) -> str:
        goals = self.plans.list_goals(chat_id)
        if not goals:
            return "Belum ada target tabungan. Buat dulu: /target liburan bali 5jt"
        found = find_amount(args)
        if not found:
            return "Format: /tabung liburan 500rb   (tarik: /tarik liburan 200rb)"
        s, e = found[1]
        name = re.sub(r"\s+", " ", args[:s] + " " + args[e:]).strip(" -:,.")
        goal = self.plans.find_goal(chat_id, name) if name else (goals[0] if len(goals) == 1 else None)
        if goal is None:
            return ("Target yang mana? " + ", ".join(g.name for g in goals) +
                    "\nContoh: /tabung " + goals[0].name + " 500rb")
        today = self.clock()
        updated = self.plans.deposit(chat_id, goal, sign * found[0], today)
        if updated is None:
            return "Tabungan di target itu tidak cukup untuk ditarik sebesar itu."
        verb = "Ditabung" if sign > 0 else "Ditarik"
        tail = "\n(Setoran ini dikurangkan dari uang aman harianmu, lihat /aman)" if sign > 0 else ""
        return f"💰 {verb} {rupiah(found[0])} → {goal.name}\n" + plans_mod.format_goal(updated, today) + tail

    def tarik_command(self, chat_id: int, args: str = "") -> str:
        return self.tabung_command(chat_id, args, sign=-1)

    def _saving_from_text(self, chat_id: int, text: str) -> str | None:
        """'nabung 500rb liburan' / 'tabung liburan 500rb'. 'tabung gas 150rb' (benda) tetap pengeluaran."""
        m = re.match(r"(?i)^\s*(nabung|menabung|tabung)\b(.*)$", text)
        if not m or find_amount(m.group(2)) is None:
            return None
        if m.group(1).lower() == "tabung":  # kata benda: hanya bila menyebut nama sebuah target
            found = find_amount(m.group(2))
            s, e = found[1]
            name = re.sub(r"\s+", " ", m.group(2)[:s] + " " + m.group(2)[e:]).strip(" -:,.")
            if not name or self.plans.find_goal(chat_id, name) is None:
                return None
        return self.tabung_command(chat_id, m.group(2))

    # ---------- utang-piutang & patungan ----------
    SPLIT_CONFIRM_SECONDS = 600
    _YES = {"ya", "y", "iya", "yes", "ok", "oke", "simpan", "gas", "siap"}
    _NO = {"batal", "tidak", "gak", "ga", "nggak", "engga", "enggak", "no", "n", "cancel"}

    def utang_command(self, chat_id: int, args: str = "") -> str:
        """/utang: daftar. /utang Budi 50rb buat bensin: AKU berutang ke Budi. /utang hapus Budi."""
        return self._debt_command(chat_id, args, debts_mod.UTANG)

    def piutang_command(self, chat_id: int, args: str = "") -> str:
        """/piutang Budi 50rb makan siang [tempo 2026-12-20]: Budi berutang PADAKU."""
        return self._debt_command(chat_id, args, debts_mod.PIUTANG)

    def _debt_command(self, chat_id: int, args: str, direction: str) -> str:
        today, text = self.clock(), args.strip()
        if not text:
            return debts_mod.format_ledger(self.debts.list(chat_id), today)
        if m := re.match(r"(?i)hapus\s+(.+)$", text):
            matches = self.debts.match_person(chat_id, m.group(1))
            if len(matches) != 1:
                return "Nama tidak ditemukan atau kurang jelas. Lihat daftar: /utang"
            n = self.debts.delete_person(chat_id, matches[0])
            return f"🗑️ Catatan utang-piutang dengan {matches[0]} dihapus ({n} catatan)."
        due, rest, bad_due = debts_mod._extract_due(text, today)
        if bad_due:
            return "Tempo tidak terbaca. Contoh: tempo 2026-12-20, tempo besok, tempo 7 hari lagi"
        found = find_amount(rest)
        amount = found[0] if found else None
        before, after = (rest[: found[1][0]], rest[found[1][1]:]) if found else (rest, "")
        # nama = kata-kata sebelum jumlah ("ke Siti 30rb"); kata sisanya + setelah jumlah jadi catatan
        words = re.sub(r"(?i)^\s*(?:ke|sama|dari|pada|kepada)\s+", "", before.strip()).split()
        # sampai 2 kata ("Mas Budi") jadi nama; bila lebih, hanya kata pertama ("Budi makan siang 5rb")
        name, _ = debts_mod._take_name(" ".join(words[:2] if len(words) <= 2 else words[:1]))
        remainder = f"{' '.join(words[len(name.split()):])} {after}" if name else after
        if not name and found:                                  # "/utang 50rb Siti": jumlah di depan
            words = after.strip().split()                       # jumlah di depan: nama = kata pertama saja
            name, _ = debts_mod._take_name(" ".join(words[:1]))
            remainder = " ".join(words[1:]) if name else ""
        if not name or amount is None:
            who = "Budi" if direction == debts_mod.PIUTANG else "Siti"
            return f"Format: /{direction} <nama> <jumlah> [catatan] [tempo 2026-12-20]\nContoh: /{direction} {who} 50rb makan siang"
        return self._record_debt(chat_id, name, direction, amount, debts_mod._clean_note(remainder), due)

    def _record_debt(self, chat_id: int, name: str, direction: str, amount: int, note: str,
                     due: date | None) -> str:
        try:
            debt = self.debts.add(chat_id, name, direction, amount, note, self.clock(), due)
        except ValueError as exc:
            return f"Tidak bisa mencatat: {exc}."
        total = sum(d.outstanding for d in self.debts.list(chat_id)
                    if d.key == debt.key and d.direction == direction)
        extra = f" (total {rupiah(total)})" if total != amount else ""
        tempo = f"\n📆 Jatuh tempo {tanggal(due)}" if due else ""
        note_line = f"\n📝 {note}" if note else ""
        if direction == debts_mod.PIUTANG:
            head = f"💸 Dicatat: {debt.person} berutang {rupiah(amount)} padamu{extra}"
        else:
            head = f"💸 Dicatat: kamu berutang {rupiah(amount)} ke {debt.person}{extra}"
        return (head + note_line + tempo + "\n(Dicatat terpisah dari pengeluaran/pemasukan.)"
                f"\nLunas: /lunas {debt.person} · Daftar: /utang")

    def lunas_command(self, chat_id: int, args: str = "") -> str:
        """/lunas Budi [20rb]: Budi membayar padaku, atau aku membayar Budi (otomatis dari catatan)."""
        found = find_amount(args)
        amount, rest = (found[0], args[: found[1][0]] + " " + args[found[1][1]:]) if found else (None, args)
        name = debts_mod.clean_person(rest)
        if not name:
            return "Format: /lunas <nama> [jumlah]. Contoh: /lunas Budi 20rb (tanpa jumlah = lunas semua)"
        return self._settle(chat_id, name, amount, None)

    def _settle(self, chat_id: int, name: str, amount: int | None, direction: str | None) -> str:
        matches = self.debts.match_person(chat_id, name)
        if not matches:
            return f"Tidak ada utang-piutang terbuka dengan '{name}'. Lihat: /utang"
        if len(matches) > 1:
            return "Maksudmu yang mana? " + ", ".join(matches)
        person = matches[0]
        directions = self.debts.directions_for(chat_id, person)
        if direction is None:
            if len(directions) > 1:
                return (f"Kamu dan {person} sama-sama punya utang. Pakai /lunas dengan arah yang jelas: "
                        f"\"{person} bayar 20rb\" (dia membayarmu) atau \"bayar utang ke {person} 20rb\" (kamu membayar).")
            direction = next(iter(directions))
        if amount is not None and not 0 < amount <= debts_mod.MAX_AMOUNT:
            return "Jumlah tidak wajar."
        try:
            result = self.debts.pay(chat_id, person, amount, self.clock(), direction)
        except ValueError:
            return "Arah pembayaran ambigu. Gunakan kalimat yang jelas."
        if result is None:
            return f"Tidak ada utang-piutang terbuka dengan {person}."
        who = f"{result.person} membayar {rupiah(result.applied)}" if result.direction == debts_mod.PIUTANG \
            else f"kamu membayar {result.person} {rupiah(result.applied)}"
        if result.remaining == 0:
            tail = "🎉 Lunas!"
        else:
            tail = f"Sisa {rupiah(result.remaining)}."
        if result.excess:
            tail += f" (Jumlahnya {rupiah(result.excess)} lebih besar dari sisa, kelebihannya tidak dicatat.)"
        return f"✅ Dicatat: {who}. {tail}"

    def tagih_command(self, chat_id: int, args: str = "") -> str:
        """/tagih Budi: draf pesan penagihan yang sopan untuk kamu salin dan kirim sendiri."""
        if not args.strip():
            return "Format: /tagih <nama>. Contoh: /tagih Budi"
        matches = self.debts.match_person(chat_id, args)
        if len(matches) != 1:
            return "Nama tidak ditemukan atau kurang jelas. Lihat daftar: /utang"
        owed = [d for d in self.debts.list(chat_id) if d.key == debts_mod.person_key(matches[0])
                and d.direction == debts_mod.PIUTANG]
        if not owed:
            return f"{matches[0]} tidak berutang padamu."
        return ("Salin dan kirim sendiri ya (aku tidak mengirim pesan ke siapa pun):\n\n"
                + debts_mod.format_tagih(matches[0], owed))

    def _debt_from_text(self, chat_id: int, text: str) -> str | None:
        """'Budi utang 50rb', 'utang ke Siti 30rb', 'Budi bayar 20rb', 'lunas Budi'. None bila bukan."""
        intent = debts_mod.parse_debt_text(text, self.clock())
        if intent is None:
            return None
        if intent.error:
            return intent.error
        if intent.kind in (debts_mod.PIUTANG, debts_mod.UTANG):
            if intent.amount is None or not intent.name:
                return None
            return self._record_debt(chat_id, intent.name, intent.kind, intent.amount, intent.note, intent.due)
        # pembayaran: hanya sah bila orangnya memang punya catatan terbuka; selain itu bukan urusan kita
        matches = self.debts.match_person(chat_id, intent.name)
        if not matches:
            return None
        directions = self.debts.directions_for(chat_id, matches[0])
        wanted = {"terima": debts_mod.PIUTANG, "bayar": debts_mod.UTANG}.get(intent.kind)
        if wanted is not None and wanted not in directions:
            return None
        return self._settle(chat_id, intent.name, intent.amount, wanted)

    # -- patungan --
    def patungan_command(self, chat_id: int, args: str = "") -> str:
        return self._split_from_text(chat_id, "patungan " + args) or ""

    def _split_from_text(self, chat_id: int, text: str) -> str | None:
        plan = debts_mod.parse_split(text, self.clock())
        if plan is None:
            return None
        if isinstance(plan, str):
            return plan
        with self._pending_lock:
            self._pending_splits[chat_id] = (plan, time.monotonic() + self.SPLIT_CONFIRM_SECONDS)
        return debts_mod.format_split_preview(plan)

    def _answer_pending_split(self, chat_id: int, text: str) -> str | None:
        word = text.strip().lower().strip(".!? ")
        if word not in self._YES and word not in self._NO:
            return None
        with self._pending_lock:
            entry = self._pending_splits.pop(chat_id, None)
        if entry is None or time.monotonic() > entry[1]:
            return None  # tidak ada yang menunggu (atau kedaluwarsa): perlakukan sebagai teks biasa
        if word in self._NO:
            return "Patungan dibatalkan, tidak ada yang disimpan."
        return self._apply_split(chat_id, entry[0])

    def _apply_split(self, chat_id: int, plan: debts_mod.SplitPlan) -> str:
        if len(self.debts.list(chat_id)) + len(plan.debts) > debts_mod.MAX_OPEN_DEBTS:
            return f"Catatan utang-piutangmu penuh (maks {debts_mod.MAX_OPEN_DEBTS}). Lunasi/hapus yang lama dulu."
        today = self.clock()
        lines = ["✅ Patungan disimpan"]
        if plan.my_share > 0:
            expense_id = self.db.add_expense(chat_id, plan.my_share, f"Patungan: {plan.description}"[:120],
                                             plan.category, today, "patungan")
            lines.append(f"• Pengeluaranmu {rupiah(plan.my_share)} · {plan.category} (ID {expense_id})")
        for name, amount, direction in plan.debts:
            self.debts.add(chat_id, name, direction, amount, f"patungan {plan.description}"[:80], today)
        if plan.debts and plan.payer == "aku":
            lines.append("• Ditagih: " + ", ".join(f"{n} {rupiah(a)}" for n, a, _ in plan.debts))
        elif plan.debts:
            lines.append(f"• Kamu berutang {rupiah(plan.my_share)} ke {plan.payer}")
        if plan.debts:
            lines.append("Lacak: /utang · Tagih: /tagih <nama>")
        return "\n".join(lines)

    # ---------- tagihan & langganan ----------
    def bills_command(self, chat_id: int, kind: str, args: str) -> str:
        today = self.clock()
        if not args.strip():
            return bills_mod.format_bill_list(self.db.list_bills(chat_id), today)
        parsed = bills_mod.parse_bill_args(args)
        if parsed is None:
            return ("Format: /{k} <nama> <jumlah> tgl <tanggal>\n"
                    "Contoh: /{k} {ex}\nTahunan: tambahkan 'bulan <1-12> tahunan'."
                    ).format(k=kind, ex="netflix 54rb tgl 5" if kind == "langganan" else "listrik 350rb tgl 20")
        bill, old, created = self.db.upsert_bill(
            chat_id, parsed.name, parsed.amount, parsed.due_day, kind, today,
            parsed.cycle, parsed.due_month, "manual",
        )
        due = bills_mod.next_due(bill, today)
        verb = "Ditambahkan" if created else "Diperbarui"
        amount = f" {rupiah(bill.amount)}" if bill.amount else ""
        return f"✅ {verb}: {kind} {bill.name}{amount}. Jatuh tempo berikutnya {tanggal(due)}."

    def confirm_command(self, chat_id: int, name: str) -> str:
        bill = self.db.find_bill(chat_id, name) if name.strip() else None
        if bill is None:
            return "Nama tidak ditemukan. Lihat daftar di /tagihan."
        self.db.confirm_bill(bill.id, self.clock())
        return f"👍 {bill.name} ditandai masih dipakai. Aku tanya lagi 90 hari lagi."

    def cancel_command(self, chat_id: int, name: str) -> str:
        bill = self.db.find_bill(chat_id, name) if name.strip() else None
        if bill is None:
            return "Nama tidak ditemukan. Lihat daftar di /tagihan."
        self.db.set_bill_active(bill.id, False)
        return (f"🗑️ {bill.name} ditandai sudah berhenti — pengingat dimatikan. "
                f"Ingat: pastikan juga sudah dibatalkan di penyedianya.")

    def daily_job(self, chat_id: int) -> list[str]:
        """Pesan harian: pengingat jatuh tempo (+ tinjauan langganan tiap tanggal 1)."""
        today = self.clock()
        messages: list[str] = []
        reminders = bills_mod.due_reminders(self.db, chat_id, today, self.reminder_days)
        if reminders:
            messages.append(bills_mod.format_reminders(reminders))
            for r in reminders:
                self.db.mark_reminded(r.bill.id, r.token)
        due_debts = debts_mod.due_reminders(self.debts, chat_id, today, self.reminder_days)
        if due_debts:
            messages.append(debts_mod.format_reminders(due_debts))
            for r in due_debts:
                self.debts.mark_reminded(chat_id, r.debt.id, r.token)
        if today.day == 1:
            stale = bills_mod.stale_subscriptions(self.db, chat_id, today)
            if stale:
                messages.append(bills_mod.format_stale(stale))
            recurring = bills_mod.detect_recurring(self.db, chat_id, today)
            if recurring:
                messages.append(bills_mod.format_recurring(recurring))
        return messages

    # ---------- email ----------
    def scan_email(self, chat_id: int) -> list[str]:
        if not self.access.is_admin(chat_id):
            # Satu kotak surat milik pemilik; mengakses email pengguna lain tidak ditawarkan.
            return ["Fitur baca email belum tersedia untuk akunmu."]
        if not (self.email_fetcher and self.llm):
            return ["Pemindai email belum aktif. Isi IMAP_* dan pengaturan AI (OLLAMA_MODEL atau ANTHROPIC_API_KEY) di .env."]
        if not self._scan_lock.acquire(blocking=False):
            return ["Pemindaian email sedang berjalan, tunggu selesai dulu."]
        try:
            findings = email_scan.scan(self.db, self.llm, chat_id, self.clock(), self.email_fetcher)
        except (imaplib.IMAP4.error, OSError) as exc:
            log.warning("Scan email gagal: %s", exc)
            return ["Gagal membuka email. Cek IMAP_USER / App Password di .env."]
        finally:
            self._scan_lock.release()
        return findings
