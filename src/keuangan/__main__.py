from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from pathlib import Path

from .bot import build_app
from .config import ConfigError, load_config
from .dashboard import DashboardServer, load_or_create_token
from .db import Database
from .email_scan import make_imap_fetcher
from .llm import make_llm
from .service import Service


def start_dashboard(cfg, db, clock) -> str | None:
    """Nyalakan dashboard lokal; kembalikan tautan masuk, atau None bila mati/gagal."""
    if not cfg.dashboard_port:
        return None
    token = cfg.dashboard_token or load_or_create_token(Path(cfg.db_path).with_name("dashboard_token"))
    try:
        server = DashboardServer(db, cfg.allowed_chat_ids, clock, token, cfg.dashboard_port)
    except OSError as exc:  # mis. port dipakai program lain: bot tetap jalan tanpa dashboard
        logging.getLogger(__name__).warning("Dashboard tidak aktif (port %s): %s", cfg.dashboard_port, exc)
        return None
    server.start()
    return server.url()


def main() -> int:
    os.makedirs("data", exist_ok=True)
    handlers: list[logging.Handler] = [logging.FileHandler("data/bot.log", encoding="utf-8")]
    if sys.stderr is not None:  # pythonw (latar belakang) tidak punya stderr
        handlers.append(logging.StreamHandler())
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        level=logging.INFO, handlers=handlers)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # URL httpx memuat token bot
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"Konfigurasi salah: {exc}", file=sys.stderr)
        return 2

    tz = ZoneInfo(cfg.timezone)
    clock = lambda: datetime.now(tz).date()  # noqa: E731
    llm = make_llm(cfg)
    db = Database(cfg.db_path)
    fetcher = (
        make_imap_fetcher(cfg.imap_host, cfg.imap_user, cfg.imap_password,
                          cfg.imap_folder, cfg.email_lookback_days, clock, db.email_seen)
        if cfg.email_enabled else None
    )
    svc = Service(db, llm, clock, cfg.reminder_days, fetcher)
    svc.dashboard_url = start_dashboard(cfg, db, clock)

    logging.getLogger(__name__).info(
        "Siap. AI=%s, scan email=%s, chat diizinkan=%d",
        cfg.llm_label, "on" if fetcher else "off", len(cfg.allowed_chat_ids),
    )
    # bootstrap_retries=-1: kalau internet putus saat start, coba lagi terus (bukan langsung mati)
    build_app(cfg, svc).run_polling(bootstrap_retries=-1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
