"""Akun pengguna: daftar via kode undangan, persetujuan privasi, kuota AI, token login dashboard
dan sesi. Semua kueri berparameter; token/sesi disimpan sebagai hash (bocor DB != bocor akses)."""
from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from .db import Database

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # tanpa 0/O/1/I/L yang membingungkan
CODE_LENGTH = 8
TOUCH_EVERY_SECONDS = 60


def normalize_code(raw: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", raw or "").upper()


def format_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}" if len(code) > 4 else code


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def clean_name(name: str) -> str:
    """Nama tampilan Telegram: tanpa karakter kontrol, maksimal 60 karakter."""
    return re.sub(r"[\x00-\x1f\x7f]", "", name or "").strip()[:60]


@dataclass
class User:
    chat_id: int
    display_name: str
    status: str  # pending | active | blocked
    role: str    # user | admin
    invite_code: str | None
    consent_version: str | None
    consent_at: str | None
    created_at: str
    last_seen: str | None


def _user(row: sqlite3.Row) -> User:
    return User(row["chat_id"], row["display_name"], row["status"], row["role"], row["invite_code"],
                row["consent_version"], row["consent_at"], row["created_at"], row["last_seen"])


class AccountStore:
    def __init__(self, db: Database, now: Callable[[], datetime] | None = None):
        self.db = db
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._last_touch: dict[int, datetime] = {}

    def _ts(self, delta: timedelta = timedelta(0)) -> str:
        return (self._now() + delta).strftime("%Y-%m-%d %H:%M:%S")

    # ---------- pengguna ----------
    def get_user(self, chat_id: int) -> User | None:
        rows = self.db.query("SELECT * FROM users WHERE chat_id=?", (chat_id,))
        return _user(rows[0]) if rows else None

    def ensure_admin(self, chat_id: int, name: str = "") -> None:
        """Admin (dari .env) selalu aktif; dibuat/diperbarui otomatis."""
        self.db.execute(
            "INSERT INTO users (chat_id, display_name, status, role, created_at, consent_at)"
            " VALUES (?, ?, 'active', 'admin', ?, ?)"
            " ON CONFLICT(chat_id) DO UPDATE SET status='active', role='admin',"
            " display_name=CASE WHEN excluded.display_name != '' THEN excluded.display_name"
            " ELSE users.display_name END",
            (chat_id, clean_name(name), self._ts(), self._ts()))

    def create_pending(self, chat_id: int, name: str, invite_code: str) -> User:
        self.db.execute(
            "INSERT INTO users (chat_id, display_name, status, role, invite_code, created_at)"
            " VALUES (?, ?, 'pending', 'user', ?, ?)"
            " ON CONFLICT(chat_id) DO NOTHING",
            (chat_id, clean_name(name), invite_code, self._ts()))
        user = self.get_user(chat_id)
        assert user is not None
        return user

    def give_consent(self, chat_id: int, version: str) -> bool:
        cur = self.db.execute(
            "UPDATE users SET status='active', consent_version=?, consent_at=?"
            " WHERE chat_id=? AND status='pending'", (version, self._ts(), chat_id))
        return cur.rowcount == 1

    def set_status(self, chat_id: int, status: str) -> bool:
        """Blokir/aktifkan pengguna biasa. Admin tidak bisa diubah lewat sini."""
        if status not in ("active", "blocked"):
            raise ValueError("status harus active atau blocked")
        cur = self.db.execute(
            "UPDATE users SET status=? WHERE chat_id=? AND role='user'"
            " AND (status != 'pending' OR ?='blocked')", (status, chat_id, status))
        if cur.rowcount:
            self.db.execute("DELETE FROM sessions WHERE chat_id=?", (chat_id,))  # cabut sesi dashboard
        return cur.rowcount == 1

    def touch(self, chat_id: int) -> None:
        """Catat 'terakhir terlihat' paling sering sekali per menit (hemat tulis)."""
        now = self._now()
        last = self._last_touch.get(chat_id)
        if last and (now - last).total_seconds() < TOUCH_EVERY_SECONDS:
            return
        self._last_touch[chat_id] = now
        self.db.execute("UPDATE users SET last_seen=? WHERE chat_id=?", (self._ts(), chat_id))

    def list_users(self, limit: int = 50) -> list[User]:
        rows = self.db.query("SELECT * FROM users ORDER BY created_at DESC, chat_id LIMIT ?", (limit,))
        return [_user(r) for r in rows]

    def active_chat_ids(self) -> list[int]:
        return [r["chat_id"] for r in self.db.query(
            "SELECT chat_id FROM users WHERE status='active' ORDER BY chat_id")]

    def counts(self) -> dict[str, int]:
        out = {"active": 0, "pending": 0, "blocked": 0}
        for r in self.db.query("SELECT status, COUNT(*) AS n FROM users GROUP BY status"):
            out[r["status"]] = r["n"]
        return out

    # ---------- undangan ----------
    def create_invite(self, created_by: int, note: str = "", max_uses: int = 1,
                      ttl_days: int | None = 7) -> str:
        if not 1 <= max_uses <= 100:
            raise ValueError("max_uses harus 1-100")
        expires = self._ts(timedelta(days=ttl_days)) if ttl_days else None
        for _ in range(10):
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
            try:
                self.db.execute(
                    "INSERT INTO invites (code, created_by, note, max_uses, expires_at, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (code, created_by, note.strip()[:80], max_uses, expires, self._ts()))
                return code
            except sqlite3.IntegrityError:
                continue  # kode kembar (sangat jarang): coba lagi
        raise RuntimeError("gagal membuat kode undangan unik")

    def redeem_invite(self, raw_code: str) -> bool:
        """Pakai satu jatah kode. Atomik: dua orang tak bisa memakai jatah terakhir bersamaan."""
        code = normalize_code(raw_code)
        if len(code) != CODE_LENGTH:
            return False
        cur = self.db.execute(
            "UPDATE invites SET uses = uses + 1 WHERE code=? AND uses < max_uses"
            " AND (expires_at IS NULL OR expires_at > ?)", (code, self._ts()))
        return cur.rowcount == 1

    def active_invites(self) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT * FROM invites WHERE uses < max_uses AND (expires_at IS NULL OR expires_at > ?)"
            " ORDER BY created_at DESC", (self._ts(),))

    # ---------- kuota AI ----------
    def ai_calls_today(self, chat_id: int, day: str) -> int:
        rows = self.db.query("SELECT calls FROM ai_usage WHERE chat_id=? AND day=?", (chat_id, day))
        return rows[0]["calls"] if rows else 0

    def add_ai_call(self, chat_id: int, day: str) -> None:
        self.db.execute(
            "INSERT INTO ai_usage (chat_id, day, calls) VALUES (?, ?, 1)"
            " ON CONFLICT(chat_id, day) DO UPDATE SET calls = calls + 1", (chat_id, day))

    # ---------- login dashboard ----------
    def create_login_token(self, chat_id: int, ttl_seconds: int = 600) -> str:
        """Token sekali pakai (berlaku singkat) untuk masuk ke dashboard."""
        token = secrets.token_urlsafe(32)
        self.db.execute("INSERT INTO login_tokens (token_hash, chat_id, expires_at) VALUES (?, ?, ?)",
                        (hash_token(token), chat_id, self._ts(timedelta(seconds=ttl_seconds))))
        return token

    def consume_login_token(self, token: str) -> int | None:
        """Tukar token jadi chat_id (sekali saja). None bila salah/kedaluwarsa/sudah dipakai."""
        h = hash_token(token)
        cur = self.db.execute(
            "UPDATE login_tokens SET used=1 WHERE token_hash=? AND used=0 AND expires_at > ?",
            (h, self._ts()))
        if cur.rowcount != 1:
            return None
        rows = self.db.query("SELECT chat_id FROM login_tokens WHERE token_hash=?", (h,))
        return rows[0]["chat_id"] if rows else None

    def create_session(self, chat_id: int, ttl_seconds: int) -> str:
        self.purge_expired()
        sid = secrets.token_urlsafe(32)
        self.db.execute(
            "INSERT INTO sessions (session_hash, chat_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
            (hash_token(sid), chat_id, self._ts(timedelta(seconds=ttl_seconds)), self._ts()))
        return sid

    def session_chat(self, sid: str) -> int | None:
        """chat_id pemilik sesi bila sesi sah DAN penggunanya masih aktif; selain itu None."""
        if not sid:
            return None
        rows = self.db.query(
            "SELECT s.chat_id FROM sessions s JOIN users u ON u.chat_id = s.chat_id"
            " WHERE s.session_hash=? AND s.expires_at > ? AND u.status='active'",
            (hash_token(sid), self._ts()))
        return rows[0]["chat_id"] if rows else None

    def delete_session(self, sid: str) -> None:
        self.db.execute("DELETE FROM sessions WHERE session_hash=?", (hash_token(sid),))

    def purge_expired(self) -> None:
        now = self._ts()
        self.db.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
        self.db.execute("DELETE FROM login_tokens WHERE expires_at <= ?", (now,))
