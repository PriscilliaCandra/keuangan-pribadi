"""Kontrol akses beta tertutup: siapa boleh memakai bot, pendaftaran, perintah admin, hapus akun,
dan kuota/antrean AI. Bebas dari Telegram (mengembalikan teks)."""
from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import date
from typing import Callable, Iterator

from . import privacy
from .accounts import AccountStore, format_code, normalize_code
from .db import Database
from .security import RateLimiter

DELETE_CONFIRM = "YA-HAPUS"
STATUS_LABEL = {"active": "aktif", "pending": "menunggu setuju", "blocked": "diblokir"}


class QuotaExceeded(Exception):
    def __init__(self, used: int, limit: int):
        super().__init__(f"kuota AI harian habis ({used}/{limit})")
        self.used, self.limit = used, limit


class AiBusy(Exception):
    """Antrean AI penuh terlalu lama."""


class AccessControl:
    def __init__(self, db: Database, admin_ids: frozenset[int] = frozenset(),
                 ai_note: str = "AI tidak aktif.", ai_daily_limit: int = 30,
                 ai_max_concurrent: int = 2, ai_wait_seconds: float = 90,
                 clock: Callable[[], date] = date.today, accounts: AccountStore | None = None):
        self.db = db
        self.admin_ids = admin_ids
        self.accounts = accounts or AccountStore(db)
        self.ai_note = ai_note
        self.ai_daily_limit = ai_daily_limit
        self.ai_wait_seconds = ai_wait_seconds
        self.clock = clock
        self.bot_username: str | None = None  # diisi bot saat start, untuk tautan undangan
        self._ai_sem = threading.BoundedSemaphore(max(1, ai_max_concurrent))
        self._code_limiter = RateLimiter(max_events=5, window_s=900)  # 5 tebakan kode / 15 menit

    # ---------- status akses ----------
    def is_admin(self, chat_id: int) -> bool:
        return chat_id in self.admin_ids

    def state(self, chat_id: int, name: str = "") -> str:
        """'ok' | 'unregistered' | 'pending' | 'blocked'."""
        if self.is_admin(chat_id):
            self.accounts.ensure_admin(chat_id, name)
            return "ok"
        user = self.accounts.get_user(chat_id)
        if user is None:
            return "unregistered"
        if user.status == "active":
            self.accounts.touch(chat_id)
            return "ok"
        return user.status  # 'pending' | 'blocked'

    def gate_message(self, state: str, chat_id: int) -> str:
        if state == "pending":
            return "Pendaftaranmu belum selesai. Baca /privasi lalu ketik /setuju untuk mengaktifkan."
        if state == "blocked":
            return "Akses akunmu dinonaktifkan. Hubungi pemilik bot bila ini keliru."
        hint = ("" if self.admin_ids else
                f"\n\n(Setup awal: Chat ID kamu {chat_id}. Isi ADMIN_CHAT_IDS di .env lalu restart.)")
        return ("Bot ini sedang beta tertutup. Kalau kamu punya kode undangan, kirim:\n"
                "/daftar KODE-UNDANGAN" + hint)

    def active_chat_ids(self) -> list[int]:
        """Semua yang menerima pengingat/ringkasan terjadwal: pengguna aktif + admin."""
        return sorted(set(self.accounts.active_chat_ids()) | set(self.admin_ids))

    def help_extra(self, chat_id: int) -> str:
        if not self.is_admin(chat_id):
            return "\n\n🔐 /privasi · /hapus_akun"
        return ("\n\n🛡️ Admin: /undang [jumlah] [catatan] · /pengguna · /blokir <id> · /aktifkan <id> · /statistik\n"
                "🔐 /privasi · /hapus_akun")

    # ---------- pendaftaran ----------
    def register(self, chat_id: int, name: str, args: str) -> str:
        if self.is_admin(chat_id):
            return "Kamu admin, tidak perlu mendaftar."
        user = self.accounts.get_user(chat_id)
        if user and user.status == "active":
            return "Kamu sudah terdaftar. Ketik /start untuk bantuan."
        if user and user.status == "blocked":
            return "Akses akunmu dinonaktifkan. Hubungi pemilik bot bila ini keliru."
        if user and user.status == "pending":
            return privacy.summary(self.ai_note)
        if self._code_limiter.blocked(chat_id):
            minutes = self._code_limiter.retry_after(chat_id) // 60 + 1
            return f"Terlalu banyak percobaan kode. Coba lagi sekitar {minutes} menit."
        code = normalize_code(args)
        if not code:
            return "Kirim: /daftar KODE-UNDANGAN"
        if not self.accounts.redeem_invite(code):
            self._code_limiter.record(chat_id)
            return "Kode tidak valid, sudah kedaluwarsa, atau sudah terpakai."
        self._code_limiter.reset(chat_id)
        self.accounts.create_pending(chat_id, name, code)
        return privacy.summary(self.ai_note)

    def consent(self, chat_id: int) -> str:
        if self.is_admin(chat_id):
            return "Kamu admin, tidak perlu persetujuan."
        user = self.accounts.get_user(chat_id)
        if user is None:
            return self.gate_message("unregistered", chat_id)
        if user.status == "blocked":
            return self.gate_message("blocked", chat_id)
        if user.status == "active":
            return "Kamu sudah aktif. Ketik /start untuk bantuan."
        self.accounts.give_consent(chat_id, privacy.VERSION)
        return "✅ Terima kasih, akunmu aktif! Ketik /start untuk melihat cara pakai."

    def privacy(self) -> str:
        return privacy.summary(self.ai_note).rsplit("\n\n", 1)[0]  # tanpa kalimat "/setuju"

    # ---------- admin ----------
    def _admin_only(self, chat_id: int) -> str | None:
        return None if self.is_admin(chat_id) else "Perintah ini khusus admin."

    def invite(self, chat_id: int, args: str) -> str:
        if (denied := self._admin_only(chat_id)):
            return denied
        parts = args.split(None, 1)
        max_uses, note = 1, args.strip()
        if parts and parts[0].isdigit():
            max_uses, note = int(parts[0]), (parts[1] if len(parts) > 1 else "")
        if not 1 <= max_uses <= 100:
            return "Jumlah pemakaian harus 1-100. Contoh: /undang 3 teman kantor"
        code = self.accounts.create_invite(chat_id, note, max_uses)
        link = f"\nTautan: https://t.me/{self.bot_username}?start={code}" if self.bot_username else ""
        return (f"🎟️ Kode undangan: {format_code(code)} (berlaku 7 hari, {max_uses}x pakai)\n"
                f"Kirim ke calon pengguna, lalu mereka ketik: /daftar {format_code(code)}{link}")

    def users(self, chat_id: int, args: str = "") -> str:
        if (denied := self._admin_only(chat_id)):
            return denied
        day = self.clock().isoformat()
        rows = ["👥 Pengguna (hanya metadata, bukan isi catatan)"]
        for u in self.accounts.list_users(50):
            seen = (u.last_seen or "-")[:10]
            role = " [admin]" if u.role == "admin" else ""
            rows.append(f"{u.chat_id} · {u.display_name or '-'}{role} · {STATUS_LABEL.get(u.status, u.status)}"
                        f" · terakhir {seen} · AI hari ini {self.accounts.ai_calls_today(u.chat_id, day)}")
        return "\n".join(rows) if len(rows) > 1 else "Belum ada pengguna."

    def _set_status(self, chat_id: int, args: str, status: str) -> str:
        if (denied := self._admin_only(chat_id)):
            return denied
        token = args.strip()
        if not token.lstrip("-").isdigit():
            return f"Format: /{'blokir' if status == 'blocked' else 'aktifkan'} <id pengguna> (lihat /pengguna)"
        target = int(token)
        if self.is_admin(target):
            return "Admin tidak bisa diubah lewat perintah ini."
        if not self.accounts.set_status(target, status):
            return "Pengguna tidak ditemukan, atau belum menyetujui privasi (status menunggu)."
        return f"{'⛔ Diblokir' if status == 'blocked' else '✅ Diaktifkan'}: {target}"

    def block(self, chat_id: int, args: str) -> str:
        return self._set_status(chat_id, args, "blocked")

    def unblock(self, chat_id: int, args: str) -> str:
        return self._set_status(chat_id, args, "active")

    def stats(self, chat_id: int, args: str = "") -> str:
        if (denied := self._admin_only(chat_id)):
            return denied
        c = self.accounts.counts()
        return (f"📊 Pengguna: {c['active']} aktif · {c['pending']} menunggu setuju · {c['blocked']} diblokir\n"
                f"Kode undangan aktif: {len(self.accounts.active_invites())}\n"
                f"Batas AI per pengguna/hari: {self.ai_daily_limit}")

    # ---------- hapus akun (hak hapus data) ----------
    def delete_account(self, chat_id: int, args: str) -> str:
        if args.strip().upper() != DELETE_CONFIRM:
            return ("⚠️ Ini menghapus SEMUA datamu secara permanen (catatan, anggaran, tagihan, akun) dan "
                    "tidak bisa dibatalkan. Unduh dulu dengan /ekspor bila perlu.\n"
                    f"Untuk melanjutkan ketik: /hapus_akun {DELETE_CONFIRM}")
        counts = self.db.delete_chat_data(chat_id)
        records = counts.get("expenses", 0) + counts.get("incomes", 0)
        note = "\n(Akun admin dibuat ulang otomatis dari .env.)" if self.is_admin(chat_id) else \
               "\nUntuk memakai lagi kamu perlu kode undangan baru."
        return f"🗑️ Selesai. {records} catatan dan seluruh data akunmu sudah dihapus.{note}"

    # ---------- kuota & antrean AI ----------
    @contextmanager
    def ai_slot(self, chat_id: int, count: bool = True) -> Iterator[None]:
        """Izin memakai AI: periksa kuota harian (admin bebas), lalu antre untuk giliran AI.

        `count=False` untuk panggilan murah di belakang layar (mis. kategorisasi cadangan): tidak
        memakai kuota harian tetapi tetap ikut antre agar server tidak kebanjiran.
        """
        day = self.clock().isoformat()
        metered = count and not self.is_admin(chat_id)
        if metered:
            used = self.accounts.ai_calls_today(chat_id, day)
            if used >= self.ai_daily_limit:
                raise QuotaExceeded(used, self.ai_daily_limit)
        if not self._ai_sem.acquire(timeout=self.ai_wait_seconds):
            raise AiBusy()
        try:
            if metered:
                self.accounts.add_ai_call(chat_id, day)
            yield
        finally:
            self._ai_sem.release()
