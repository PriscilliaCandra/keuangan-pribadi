from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from .access import AccessControl
from .bot import build_app
from .config import Config, ConfigError, load_config
from .dashboard_server import DashboardServer
from .db import Database
from .email_scan import make_imap_fetcher
from .llm import make_llm
from .service import Service


def start_dashboard(cfg: Config, db: Database, access: AccessControl, clock) -> str | None:
    """Nyalakan dashboard; kembalikan alamat dasar untuk tautan login, atau None bila mati/gagal."""
    if not cfg.dashboard_port:
        return None
    base = cfg.dashboard_base_url
    try:
        server = DashboardServer(
            db, access.accounts, clock, port=cfg.dashboard_port,
            extra_hosts=cfg.dashboard_host_names, secure_cookie=base.startswith("https://"),
            session_ttl=cfg.dashboard_session_days * 86400, trust_proxy=cfg.dashboard_trust_proxy)
    except OSError as exc:  # mis. port dipakai program lain: bot tetap jalan tanpa dashboard
        logging.getLogger(__name__).warning("Dashboard tidak aktif (port %s): %s", cfg.dashboard_port, exc)
        return None
    server.start()
    return base


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
    access = AccessControl(db, cfg.admin_chat_ids, cfg.ai_privacy_note, cfg.ai_daily_limit,
                           cfg.ai_max_concurrent, cfg.ai_wait_seconds, clock)
    svc = Service(db, llm, clock, cfg.reminder_days, fetcher, access)
    svc.dashboard_base_url = start_dashboard(cfg, db, access, clock)

    logging.getLogger(__name__).info(
        "Siap. AI=%s, scan email=%s, admin=%d, dashboard=%s, kuota AI=%d/hari",
        cfg.llm_label, "on" if fetcher else "off", len(cfg.admin_chat_ids),
        "on" if svc.dashboard_base_url else "off", cfg.ai_daily_limit,
    )
    # bootstrap_retries=-1: kalau internet putus saat start, coba lagi terus (bukan langsung mati)
    build_app(cfg, svc).run_polling(bootstrap_retries=-1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
