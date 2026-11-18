import sqlite3
from datetime import datetime, timedelta

from keuangan.backup import run_backup
from keuangan.db import Database
from tests.conftest import TODAY


def test_backup_is_a_consistent_openable_copy(tmp_path):
    db = Database(str(tmp_path / "live.db"))
    db.add_expense(1, 25_000, "kopi", "Makanan & Minuman", TODAY)
    db.add_income(1, 8_000_000, "gaji", TODAY)
    path = run_backup(db, tmp_path / "bak", keep=3, now=lambda: datetime(2026, 11, 18, 3, 30))
    assert path.name == "keuangan-20261118-033000.db"
    copy = Database(str(path))
    assert copy.sum_between(1, TODAY, TODAY + timedelta(days=1)) == 25_000
    assert copy.sum_income_between(1, TODAY, TODAY + timedelta(days=1)) == 8_000_000
    # menulis ke database asli setelahnya tidak mengubah cadangan
    db.add_expense(1, 1_000, "x", "Belanja", TODAY)
    assert copy.sum_between(1, TODAY, TODAY + timedelta(days=1)) == 25_000


def test_backup_rotation_keeps_only_the_newest(tmp_path):
    db = Database(str(tmp_path / "live.db"))
    for day in range(1, 8):
        run_backup(db, tmp_path / "bak", keep=3, now=lambda d=day: datetime(2026, 11, d, 3, 30))
    names = sorted(p.name for p in (tmp_path / "bak").glob("*.db"))
    assert names == ["keuangan-20261105-033000.db", "keuangan-20261106-033000.db",
                     "keuangan-20261107-033000.db"]


def test_rotation_ignores_unrelated_files(tmp_path):
    db = Database(str(tmp_path / "live.db"))
    folder = tmp_path / "bak"
    folder.mkdir()
    (folder / "catatan.txt").write_text("jangan dihapus")
    for day in range(1, 5):
        run_backup(db, folder, keep=1, now=lambda d=day: datetime(2026, 11, d, 3, 30))
    assert (folder / "catatan.txt").exists() and len(list(folder.glob("keuangan-*.db"))) == 1


def test_wal_mode_enabled_for_file_databases(tmp_path):
    db = Database(str(tmp_path / "live.db"))
    assert db.query("PRAGMA journal_mode")[0][0].lower() == "wal"
    assert Database(":memory:").query("PRAGMA journal_mode")[0][0].lower() == "memory"


def test_delete_chat_data_counts_and_scopes(db):
    db.add_expense(1, 1000, "a", "Belanja", TODAY)
    db.add_expense(2, 2000, "b", "Belanja", TODAY)
    counts = db.delete_chat_data(1)
    assert counts["expenses"] == 1 and db.sum_between(2, TODAY, TODAY + timedelta(days=1)) == 2000
