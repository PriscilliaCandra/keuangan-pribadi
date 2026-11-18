from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .categories import guess_income_category

SCHEMA = """
CREATE TABLE IF NOT EXISTS expenses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER NOT NULL,
  amount INTEGER NOT NULL CHECK (amount > 0),
  description TEXT NOT NULL,
  category TEXT NOT NULL,
  spent_on TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'teks',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_expenses_chat_date ON expenses(chat_id, spent_on);

CREATE TABLE IF NOT EXISTS incomes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER NOT NULL,
  amount INTEGER NOT NULL CHECK (amount > 0),
  description TEXT NOT NULL,
  category TEXT NOT NULL DEFAULT 'Lainnya',
  received_on TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'teks',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now'))
);
CREATE INDEX IF NOT EXISTS ix_incomes_chat_date ON incomes(chat_id, received_on);

CREATE TABLE IF NOT EXISTS budgets (
  chat_id INTEGER NOT NULL,
  category TEXT NOT NULL,
  monthly_limit INTEGER NOT NULL CHECK (monthly_limit > 0),
  PRIMARY KEY (chat_id, category)
);

CREATE TABLE IF NOT EXISTS budget_alerts (
  chat_id INTEGER NOT NULL,
  category TEXT NOT NULL,
  month TEXT NOT NULL,
  level INTEGER NOT NULL,
  PRIMARY KEY (chat_id, category, month, level)
);

CREATE TABLE IF NOT EXISTS bills (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  name_key TEXT NOT NULL,
  amount INTEGER,
  due_day INTEGER NOT NULL CHECK (due_day BETWEEN 1 AND 31),
  due_month INTEGER CHECK (due_month BETWEEN 1 AND 12),
  kind TEXT NOT NULL CHECK (kind IN ('tagihan', 'langganan')),
  cycle TEXT NOT NULL DEFAULT 'monthly' CHECK (cycle IN ('monthly', 'yearly')),
  active INTEGER NOT NULL DEFAULT 1,
  source TEXT NOT NULL DEFAULT 'manual',
  last_confirmed TEXT,
  last_reminded TEXT,
  created_on TEXT NOT NULL,
  UNIQUE (chat_id, name_key)
);

CREATE TABLE IF NOT EXISTS processed_emails (
  message_id TEXT PRIMARY KEY,
  chat_id INTEGER NOT NULL,
  processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS users (
  chat_id INTEGER PRIMARY KEY,
  display_name TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'blocked')),
  role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'admin')),
  invite_code TEXT,
  consent_version TEXT,
  consent_at TEXT,
  created_at TEXT NOT NULL,
  last_seen TEXT
);

CREATE TABLE IF NOT EXISTS invites (
  code TEXT PRIMARY KEY,
  created_by INTEGER NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  max_uses INTEGER NOT NULL DEFAULT 1 CHECK (max_uses > 0),
  uses INTEGER NOT NULL DEFAULT 0,
  expires_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_usage (
  chat_id INTEGER NOT NULL,
  day TEXT NOT NULL,
  calls INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (chat_id, day)
);

CREATE TABLE IF NOT EXISTS login_tokens (
  token_hash TEXT PRIMARY KEY,
  chat_id INTEGER NOT NULL,
  expires_at TEXT NOT NULL,
  used INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sessions (
  session_hash TEXT PRIMARY KEY,
  chat_id INTEGER NOT NULL,
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_settings (
  chat_id INTEGER PRIMARY KEY,
  payday INTEGER CHECK (payday BETWEEN 1 AND 31),
  evening_reminder INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS recurring (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  name_key TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('keluar', 'masuk')),
  amount INTEGER NOT NULL CHECK (amount > 0),
  category TEXT NOT NULL,
  day INTEGER NOT NULL CHECK (day BETWEEN 1 AND 31),
  active INTEGER NOT NULL DEFAULT 1,
  last_run_month TEXT,
  created_on TEXT NOT NULL,
  UNIQUE (chat_id, name_key)
);

CREATE TABLE IF NOT EXISTS goals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  name_key TEXT NOT NULL,
  target INTEGER NOT NULL CHECK (target > 0),
  deadline TEXT,
  created_on TEXT NOT NULL,
  UNIQUE (chat_id, name_key)
);

CREATE TABLE IF NOT EXISTS goal_deposits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  goal_id INTEGER NOT NULL,
  chat_id INTEGER NOT NULL,
  amount INTEGER NOT NULL CHECK (amount != 0),
  on_date TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_goal_deposits_chat_date ON goal_deposits(chat_id, on_date);

CREATE TABLE IF NOT EXISTS debts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER NOT NULL,
  person TEXT NOT NULL,
  person_key TEXT NOT NULL,
  direction TEXT NOT NULL CHECK (direction IN ('piutang', 'utang')),
  amount INTEGER NOT NULL CHECK (amount > 0),
  note TEXT NOT NULL DEFAULT '',
  created_on TEXT NOT NULL,
  due_on TEXT,
  last_reminded TEXT
);
CREATE INDEX IF NOT EXISTS ix_debts_chat_person ON debts(chat_id, person_key);

CREATE TABLE IF NOT EXISTS debt_payments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  debt_id INTEGER NOT NULL,
  chat_id INTEGER NOT NULL,
  amount INTEGER NOT NULL CHECK (amount > 0),
  on_date TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_debt_payments_debt ON debt_payments(debt_id);
"""

# Tabel yang berisi data milik pengguna (kunci chat_id): dihapus saat pengguna minta hapus akun.
# Pengecualian disengaja: `ai_usage` (hanya chat_id + tanggal + hitungan, tanpa isi) disimpan sampai
# 7 hari agar kuota AI harian tak bisa direset dengan hapus-akun lalu daftar ulang; lihat privacy.py.
USER_DATA_TABLES = ("expenses", "incomes", "budgets", "budget_alerts", "bills",
                    "goal_deposits", "goals", "user_settings", "debt_payments", "debts", "recurring",
                    "processed_emails", "login_tokens", "sessions", "users")


@dataclass
class Expense:
    id: int
    chat_id: int
    amount: int
    description: str
    category: str
    spent_on: date
    source: str


@dataclass
class Income:
    id: int
    chat_id: int
    amount: int
    description: str
    received_on: date
    source: str
    category: str = "Lainnya"


@dataclass
class Bill:
    id: int
    chat_id: int
    name: str
    amount: int | None
    due_day: int
    due_month: int | None
    kind: str
    cycle: str
    active: bool
    source: str
    last_confirmed: date | None
    last_reminded: str | None
    created_on: date


def _d(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


class Database:
    def __init__(self, path: str = ":memory:"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._last_stamp: datetime | None = None
        with self._lock:
            self._conn.execute("PRAGMA busy_timeout = 5000")
            if path != ":memory:":
                self._conn.execute("PRAGMA journal_mode = WAL")  # baca tak memblokir tulis
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    # ---------- pembantu umum (dipakai accounts.py) ----------
    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        """Jalankan satu perintah tulis di dalam kunci lalu commit. Nilai selalu lewat `params`."""
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def query(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def delete_chat_data(self, chat_id: int) -> dict[str, int]:
        """Hapus SEMUA data satu pengguna (hak hapus data). Mengembalikan jumlah baris per tabel."""
        counts: dict[str, int] = {}
        with self._lock:
            for table in USER_DATA_TABLES:  # nama tabel dari konstanta, bukan input
                counts[table] = self._conn.execute(
                    f"DELETE FROM {table} WHERE chat_id=?", (chat_id,)).rowcount
            self._conn.commit()
        return counts

    def backup_to(self, dest_path: str) -> None:
        """Salinan konsisten database yang sedang dipakai (aman dijalankan saat bot hidup)."""
        Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
        dest = sqlite3.connect(dest_path)
        try:
            with self._lock:
                self._conn.backup(dest)
        finally:
            dest.close()

    def _migrate(self) -> None:
        """Migrasi ringan untuk database yang dibuat versi lama."""
        settings_cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(user_settings)")}
        if settings_cols and "evening_reminder" not in settings_cols:
            self._conn.execute(
                "ALTER TABLE user_settings ADD COLUMN evening_reminder INTEGER NOT NULL DEFAULT 0")
        columns = {r["name"] for r in self._conn.execute("PRAGMA table_info(incomes)")}
        if "category" not in columns:
            self._conn.execute(
                "ALTER TABLE incomes ADD COLUMN category TEXT NOT NULL DEFAULT 'Lainnya'")
            for row in self._conn.execute("SELECT id, description FROM incomes").fetchall():
                self._conn.execute("UPDATE incomes SET category=? WHERE id=?",
                                   (guess_income_category(row["description"]), row["id"]))

    def _stamp(self) -> str:
        """Cap waktu pencatatan yang selalu naik (mikrodetik), dipakai mengurutkan riwayat
        gabungan pengeluaran + pemasukan. Dipanggil di dalam kunci."""
        now = datetime.now()
        if self._last_stamp is not None and now <= self._last_stamp:
            now = self._last_stamp + timedelta(microseconds=1)
        self._last_stamp = now
        return now.strftime("%Y-%m-%d %H:%M:%S.%f")

    # ---------- pengeluaran ----------
    def add_expense(self, chat_id: int, amount: int, description: str,
                    category: str, spent_on: date, source: str = "teks") -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO expenses (chat_id, amount, description, category, spent_on, source,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (chat_id, amount, description, category, spent_on.isoformat(), source,
                 self._stamp()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    @staticmethod
    def _expense(row: sqlite3.Row) -> Expense:
        return Expense(row["id"], row["chat_id"], row["amount"], row["description"],
                       row["category"], date.fromisoformat(row["spent_on"]), row["source"])

    def get_expense(self, chat_id: int, expense_id: int) -> Expense | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM expenses WHERE chat_id=? AND id=?", (chat_id, expense_id)
            ).fetchone()
        return self._expense(row) if row else None

    def delete_expense(self, chat_id: int, expense_id: int) -> Expense | None:
        found = self.get_expense(chat_id, expense_id)
        if found:
            with self._lock:
                self._conn.execute("DELETE FROM expenses WHERE chat_id=? AND id=?",
                                   (chat_id, expense_id))
                self._conn.commit()
        return found

    _EXPENSE_FIELDS = frozenset({"amount", "description", "category", "spent_on"})
    _INCOME_FIELDS = frozenset({"amount", "description", "category", "received_on"})

    def _update(self, table: str, allowed: frozenset[str], chat_id: int, row_id: int,
                fields: dict) -> bool:
        # Nama tabel/kolom hanya dari konstanta di atas, nilai selalu lewat parameter.
        if not fields or not set(fields) <= allowed:
            raise ValueError(f"kolom tidak boleh diubah: {sorted(set(fields) - allowed)}")
        values = [v.isoformat() if isinstance(v, date) else v for v in fields.values()]
        sql = (f"UPDATE {table} SET " + ", ".join(f"{k}=?" for k in fields)
               + " WHERE chat_id=? AND id=?")
        with self._lock:
            cur = self._conn.execute(sql, [*values, chat_id, row_id])
            self._conn.commit()
            return cur.rowcount > 0

    def update_expense(self, chat_id: int, expense_id: int, **fields) -> bool:
        return self._update("expenses", self._EXPENSE_FIELDS, chat_id, expense_id, fields)

    # ---------- pemasukan ----------
    @staticmethod
    def _income(row: sqlite3.Row) -> Income:
        return Income(row["id"], row["chat_id"], row["amount"], row["description"],
                      date.fromisoformat(row["received_on"]), row["source"], row["category"])

    def add_income(self, chat_id: int, amount: int, description: str, received_on: date,
                   source: str = "teks", category: str | None = None) -> int:
        """`category` kosong -> ditebak dari deskripsi (gaji, bonus, freelance, ...)."""
        category = category or guess_income_category(description)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO incomes (chat_id, amount, description, category, received_on, source,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (chat_id, amount, description, category, received_on.isoformat(), source,
                 self._stamp()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def get_income(self, chat_id: int, income_id: int) -> Income | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM incomes WHERE chat_id=? AND id=?", (chat_id, income_id)
            ).fetchone()
        return self._income(row) if row else None

    def delete_income(self, chat_id: int, income_id: int) -> Income | None:
        found = self.get_income(chat_id, income_id)
        if found:
            with self._lock:
                self._conn.execute("DELETE FROM incomes WHERE chat_id=? AND id=?",
                                   (chat_id, income_id))
                self._conn.commit()
        return found

    def update_income(self, chat_id: int, income_id: int, **fields) -> bool:
        return self._update("incomes", self._INCOME_FIELDS, chat_id, income_id, fields)

    def incomes_between(self, chat_id: int, start: date, end_exclusive: date) -> list[Income]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM incomes WHERE chat_id=? AND received_on >= ? AND received_on < ?"
                " ORDER BY received_on, id",
                (chat_id, start.isoformat(), end_exclusive.isoformat()),
            ).fetchall()
        return [self._income(r) for r in rows]

    def sum_income_between(self, chat_id: int, start: date, end_exclusive: date) -> int:
        with self._lock:
            return int(self._conn.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM incomes"
                " WHERE chat_id=? AND received_on >= ? AND received_on < ?",
                (chat_id, start.isoformat(), end_exclusive.isoformat()),
            ).fetchone()[0])

    # ---------- riwayat gabungan (urut waktu dicatat) ----------
    def recent_entries(self, chat_id: int, limit: int = 10) -> list[tuple[str, Expense | Income]]:
        """Catatan terakhir, pengeluaran ('keluar') dan pemasukan ('masuk') bercampur."""
        with self._lock:
            ex = self._conn.execute(
                "SELECT *, 'keluar' AS kind FROM expenses WHERE chat_id=?"
                " ORDER BY created_at DESC, id DESC LIMIT ?", (chat_id, limit)).fetchall()
            inc = self._conn.execute(
                "SELECT *, 'masuk' AS kind FROM incomes WHERE chat_id=?"
                " ORDER BY created_at DESC, id DESC LIMIT ?", (chat_id, limit)).fetchall()
        rows = sorted([*ex, *inc], key=lambda r: (r["created_at"], r["id"]), reverse=True)[:limit]
        return [("keluar", self._expense(r)) if r["kind"] == "keluar" else ("masuk", self._income(r))
                for r in rows]

    def count_chat_entries_since(self, chat_id: int, since_iso: str) -> int:
        """Jumlah catatan (pengeluaran + pemasukan) satu pengguna yang DIBUAT sejak `since_iso`."""
        total = 0
        for table in ("expenses", "incomes"):  # nama tabel dari konstanta
            total += self.query(
                f"SELECT COUNT(*) AS n FROM {table} WHERE chat_id=? AND created_at >= ?",
                (chat_id, since_iso))[0]["n"]
        return total

    def count_entries_since(self, days: int) -> int:
        """Jumlah catatan (pengeluaran + pemasukan, semua pengguna) dibuat dalam `days` hari terakhir.
        Hanya hitungan agregat untuk statistik admin; tidak membuka isi."""
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S.%f")
        total = 0
        for table in ("expenses", "incomes"):  # nama tabel dari konstanta
            total += self.query(f"SELECT COUNT(*) AS n FROM {table} WHERE created_at >= ?", (cutoff,))[0]["n"]
        return total

    def delete_latest_entry(self, chat_id: int) -> tuple[str, Expense | Income] | None:
        recent = self.recent_entries(chat_id, 1)
        if not recent:
            return None
        kind, entry = recent[0]
        (self.delete_expense if kind == "keluar" else self.delete_income)(chat_id, entry.id)
        return kind, entry

    def expenses_between(self, chat_id: int, start: date, end_exclusive: date) -> list[Expense]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM expenses WHERE chat_id=? AND spent_on >= ? AND spent_on < ?"
                " ORDER BY spent_on, id",
                (chat_id, start.isoformat(), end_exclusive.isoformat()),
            ).fetchall()
        return [self._expense(r) for r in rows]

    def sum_between(self, chat_id: int, start: date, end_exclusive: date,
                    category: str | None = None) -> int:
        sql = ("SELECT COALESCE(SUM(amount), 0) FROM expenses"
               " WHERE chat_id=? AND spent_on >= ? AND spent_on < ?")
        params: list = [chat_id, start.isoformat(), end_exclusive.isoformat()]
        if category:
            sql += " AND category=?"
            params.append(category)
        with self._lock:
            return int(self._conn.execute(sql, params).fetchone()[0])

    # ---------- anggaran ----------
    def set_budget(self, chat_id: int, category: str, limit: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO budgets (chat_id, category, monthly_limit) VALUES (?, ?, ?)"
                " ON CONFLICT(chat_id, category) DO UPDATE SET monthly_limit=excluded.monthly_limit",
                (chat_id, category, limit),
            )
            # batas berubah -> izinkan peringatan baru di bulan ini
            self._conn.execute(
                "DELETE FROM budget_alerts WHERE chat_id=? AND category=?", (chat_id, category)
            )
            self._conn.commit()

    def delete_budget(self, chat_id: int, category: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM budgets WHERE chat_id=? AND category=?", (chat_id, category)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def get_budgets(self, chat_id: int) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT category, monthly_limit FROM budgets WHERE chat_id=?", (chat_id,)
            ).fetchall()
        return {r["category"]: r["monthly_limit"] for r in rows}

    def try_mark_alert(self, chat_id: int, category: str, month: str, level: int) -> bool:
        """True kalau peringatan ini belum pernah dikirim (dan langsung dicatat)."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO budget_alerts (chat_id, category, month, level)"
                " VALUES (?, ?, ?, ?)",
                (chat_id, category, month, level),
            )
            self._conn.commit()
            return cur.rowcount > 0

    # ---------- tagihan & langganan ----------
    @staticmethod
    def _bill(row: sqlite3.Row) -> Bill:
        return Bill(
            id=row["id"], chat_id=row["chat_id"], name=row["name"], amount=row["amount"],
            due_day=row["due_day"], due_month=row["due_month"], kind=row["kind"],
            cycle=row["cycle"], active=bool(row["active"]), source=row["source"],
            last_confirmed=_d(row["last_confirmed"]), last_reminded=row["last_reminded"],
            created_on=date.fromisoformat(row["created_on"]),
        )

    def upsert_bill(self, chat_id: int, name: str, amount: int | None, due_day: int,
                    kind: str, today: date, cycle: str = "monthly",
                    due_month: int | None = None, source: str = "manual"
                    ) -> tuple[Bill, int | None, bool]:
        """Simpan/perbarui tagihan. Mengembalikan (bill, jumlah_lama, baru_dibuat).

        Entri manual mengaktifkan kembali tagihan; entri dari email tidak
        (supaya langganan yang sudah kamu batalkan tetap ketahuan kalau masih menagih).
        """
        key = name.strip().lower()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM bills WHERE chat_id=? AND name_key=?", (chat_id, key)
            ).fetchone()
            if row is None:
                cur = self._conn.execute(
                    "INSERT INTO bills (chat_id, name, name_key, amount, due_day, due_month,"
                    " kind, cycle, source, created_on) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (chat_id, name.strip(), key, amount, due_day, due_month, kind, cycle,
                     source, today.isoformat()),
                )
                self._conn.commit()
                return self.get_bill(int(cur.lastrowid)), None, True
            old_amount = row["amount"]
            active = 1 if source == "manual" else row["active"]
            self._conn.execute(
                "UPDATE bills SET amount=COALESCE(?, amount), due_day=?, due_month=?,"
                " kind=?, cycle=?, active=? WHERE id=?",
                (amount, due_day, due_month, kind, cycle, active, row["id"]),
            )
            self._conn.commit()
            return self.get_bill(row["id"]), old_amount, False

    def get_bill(self, bill_id: int) -> Bill:
        with self._lock:
            row = self._conn.execute("SELECT * FROM bills WHERE id=?", (bill_id,)).fetchone()
        return self._bill(row)

    def list_bills(self, chat_id: int, active_only: bool = True) -> list[Bill]:
        sql = "SELECT * FROM bills WHERE chat_id=?"
        if active_only:
            sql += " AND active=1"
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY due_day, name", (chat_id,)).fetchall()
        return [self._bill(r) for r in rows]

    def find_bill(self, chat_id: int, name: str) -> Bill | None:
        key = name.strip().lower()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM bills WHERE chat_id=? AND name_key=?", (chat_id, key)
            ).fetchone()
            if row is None:  # cocokkan sebagian: "netf" -> "netflix"
                row = self._conn.execute(
                    "SELECT * FROM bills WHERE chat_id=? AND name_key LIKE ? ORDER BY active DESC",
                    (chat_id, f"%{key}%"),
                ).fetchone()
        return self._bill(row) if row else None

    def set_bill_active(self, bill_id: int, active: bool) -> None:
        with self._lock:
            self._conn.execute("UPDATE bills SET active=? WHERE id=?", (int(active), bill_id))
            self._conn.commit()

    def confirm_bill(self, bill_id: int, today: date) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE bills SET last_confirmed=? WHERE id=?", (today.isoformat(), bill_id)
            )
            self._conn.commit()

    def mark_reminded(self, bill_id: int, token: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE bills SET last_reminded=? WHERE id=?", (token, bill_id))
            self._conn.commit()

    # ---------- email ----------
    def email_seen(self, message_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM processed_emails WHERE message_id=?", (message_id,)
            ).fetchone()
        return row is not None

    def mark_email(self, message_id: str, chat_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO processed_emails (message_id, chat_id) VALUES (?, ?)",
                (message_id, chat_id),
            )
            self._conn.commit()
