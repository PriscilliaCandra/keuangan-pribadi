from keuangan.bot import build_app
from keuangan.config import load_config
from keuangan.service import Service


def test_build_app_registers_handlers_and_jobs(db):
    cfg = load_config({"TELEGRAM_BOT_TOKEN": "123456:TEST", "ALLOWED_CHAT_IDS": "1"})
    app = build_app(cfg, Service(db))
    names = {j.name for j in app.job_queue.jobs()}
    assert {"harian", "mingguan"} <= names and "email" not in names
    commands = {c for h in app.handlers[0] for c in getattr(h, "commands", [])}
    assert {"anggaran", "ringkasan", "tagihan", "langganan", "pakai", "batal", "scan_email", "hapus",
            "masuk", "saldo", "riwayat", "edit", "ekspor", "tanya"} <= commands


def test_email_job_scheduled_when_configured(db):
    cfg = load_config({
        "TELEGRAM_BOT_TOKEN": "123456:TEST", "ANTHROPIC_API_KEY": "k",
        "IMAP_HOST": "h", "IMAP_USER": "u", "IMAP_PASSWORD": "p",
    })
    app = build_app(cfg, Service(db))
    assert "email" in {j.name for j in app.job_queue.jobs()}
