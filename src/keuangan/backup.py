"""Cadangan harian database SQLite (online, konsisten) dengan rotasi."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Callable

from .db import Database

PREFIX, SUFFIX = "keuangan-", ".db"


def run_backup(db: Database, dest_dir: str | Path, keep: int = 14,
               now: Callable[[], datetime] = datetime.now) -> Path:
    """Buat satu cadangan baru lalu hapus yang lebih lama dari `keep` terbaru."""
    folder = Path(dest_dir)
    path = folder / f"{PREFIX}{now():%Y%m%d-%H%M%S}{SUFFIX}"
    db.backup_to(str(path))
    try:
        os.chmod(path, 0o600)  # berisi data keuangan: hanya pemilik (diabaikan di Windows)
    except OSError:
        pass
    old = sorted(folder.glob(f"{PREFIX}*{SUFFIX}"))[:-keep] if keep > 0 else []
    for stale in old:
        stale.unlink(missing_ok=True)
    return path
