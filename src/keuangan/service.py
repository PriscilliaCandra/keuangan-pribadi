"""Logika aplikasi, bebas dari Telegram. Adapter kanal (Telegram, WhatsApp, ...) cukup memanggil
metode di sini dan mengirim string hasilnya."""
from __future__ import annotations

import imaplib
import logging
import re
import threading
from datetime import date, timedelta
from typing import Any, Callable

from . import bills as bills_mod
from . import agent, budget, email_scan, ledger, reports
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
            return budget.format_statuses(budget.statuses(self.db, chat_id, today))
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
        return "\n\n".join(parts)

    def weekly_job(self, chat_id: int) -> str:
        return self.summary_command(chat_id, "minggu")

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
