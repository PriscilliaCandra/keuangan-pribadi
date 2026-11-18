"""Adapter Telegram: hanya transport. Semua logika ada di service.py."""
from __future__ import annotations

import asyncio
import io
import logging
import uuid
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

from .config import Config
from .parser import ParsedExpense
from .service import HELP, Service

log = logging.getLogger(__name__)
MAX_PENDING = 20


def _svc(context: ContextTypes.DEFAULT_TYPE) -> Service:
    return context.application.bot_data["svc"]


def _cfg(context: ContextTypes.DEFAULT_TYPE) -> Config:
    return context.application.bot_data["cfg"]


async def _allowed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    if chat is None:
        return False
    cfg = _cfg(context)
    if chat.id in cfg.allowed_chat_ids:
        return True
    if not cfg.allowed_chat_ids and update.effective_message:
        # Mode setup: bantu user menemukan Chat ID-nya, tapi tidak memproses apa pun.
        await update.effective_message.reply_text(
            f"Chat ID kamu: {chat.id}\nTambahkan ke ALLOWED_CHAT_IDS di .env lalu restart bot."
        )
    else:
        log.warning("Chat tidak diizinkan: %s", chat.id)
    return False


async def _reply(update: Update, text: str, **kw) -> None:
    await update.effective_message.reply_text(text, **kw)


def _args(context: ContextTypes.DEFAULT_TYPE) -> str:
    return " ".join(context.args or [])


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _allowed(update, context):
        await _reply(update, HELP)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _allowed(update, context):
        return
    text = update.effective_message.text or ""
    await update.effective_chat.send_action(ChatAction.TYPING)
    replied = update.effective_message.reply_to_message
    reply_to = replied.text if replied and replied.from_user and replied.from_user.is_bot else None
    reply = await asyncio.to_thread(_svc(context).handle_text, update.effective_chat.id, text,
                                    reply_to)
    await _reply(update, reply)


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _allowed(update, context):
        return
    svc = _svc(context)
    photo = update.effective_message.photo[-1]
    file = await photo.get_file()
    data = bytes(await file.download_as_bytearray())
    parsed, message = await asyncio.to_thread(svc.read_receipt, data, "image/jpeg")
    if parsed is None:
        await _reply(update, message)
        return
    pending: dict[str, ParsedExpense] = context.chat_data.setdefault("pending", {})
    if len(pending) >= MAX_PENDING:
        pending.pop(next(iter(pending)))
    token = uuid.uuid4().hex[:8]
    pending[token] = parsed
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Simpan", callback_data=f"save:{token}"),
        InlineKeyboardButton("❌ Batal", callback_data=f"drop:{token}"),
    ]])
    await _reply(update, message, reply_markup=keyboard)


async def on_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not await _allowed(update, context):
        return
    await query.answer()
    action, _, token = (query.data or "").partition(":")
    parsed = context.chat_data.get("pending", {}).pop(token, None)
    if parsed is None:
        await query.edit_message_text("Struk ini sudah diproses atau kedaluwarsa.")
    elif action == "save":
        text = await asyncio.to_thread(
            _svc(context).save_expense, update.effective_chat.id, parsed, "struk"
        )
        await query.edit_message_text(text)
    else:
        await query.edit_message_text("Dibatalkan, tidak disimpan.")


def _command(method: str, *, kind: str | None = None):
    """Buat handler perintah yang meneruskan argumen ke Service.<method>."""

    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await _allowed(update, context):
            return
        svc, chat_id = _svc(context), update.effective_chat.id
        fn = getattr(svc, method)
        args = (chat_id, kind, _args(context)) if kind else (chat_id, _args(context))
        await update.effective_chat.send_action(ChatAction.TYPING)  # jawaban AI bisa lama
        await _reply(update, await asyncio.to_thread(fn, *args))

    return handler


async def cmd_ekspor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _allowed(update, context):
        return
    result = await asyncio.to_thread(_svc(context).export_command, update.effective_chat.id,
                                     _args(context))
    if isinstance(result, str):
        await _reply(update, result)
        return
    filename, data = result
    await update.effective_message.reply_document(
        document=io.BytesIO(data), filename=filename,
        caption="📤 Buka dengan Excel/Google Sheets. Kolom: jenis, id, tanggal, jumlah, kategori, catatan, sumber.",
    )


async def cmd_scan_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _allowed(update, context):
        return
    await _reply(update, "Memeriksa email…")
    findings = await asyncio.to_thread(_svc(context).scan_email, update.effective_chat.id)
    await _reply(update, "\n".join(findings) if findings else "Tidak ada tagihan/langganan baru di email.")


# ---------- jadwal ----------
async def _broadcast(context: ContextTypes.DEFAULT_TYPE, texts: list[str], chat_id: int) -> None:
    for text in texts:
        await context.bot.send_message(chat_id, text)


async def job_daily(context: ContextTypes.DEFAULT_TYPE) -> None:
    for chat_id in _cfg(context).allowed_chat_ids:
        texts = await asyncio.to_thread(_svc(context).daily_job, chat_id)
        await _broadcast(context, texts, chat_id)


async def job_weekly(context: ContextTypes.DEFAULT_TYPE) -> None:
    tz = ZoneInfo(_cfg(context).timezone)
    if datetime.now(tz).weekday() != 6:  # hanya hari Minggu
        return
    for chat_id in _cfg(context).allowed_chat_ids:
        text = await asyncio.to_thread(_svc(context).weekly_job, chat_id)
        await context.bot.send_message(chat_id, text)


async def job_email(context: ContextTypes.DEFAULT_TYPE) -> None:
    for chat_id in _cfg(context).allowed_chat_ids:
        findings = await asyncio.to_thread(_svc(context).scan_email, chat_id)
        await _broadcast(context, findings, chat_id)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Error tak tertangani", exc_info=context.error)


def build_app(cfg: Config, svc: Service) -> Application:
    # Jaringan rumahan sering lambat ke Telegram: timeout longgar + paksa IPv4 (IPv6 kadang macet).
    ipv4 = {"transport": httpx.AsyncHTTPTransport(local_address="0.0.0.0")}
    request = HTTPXRequest(connect_timeout=20, read_timeout=30, write_timeout=30,
                           pool_timeout=20, httpx_kwargs=ipv4)
    polling_request = HTTPXRequest(connect_timeout=20, read_timeout=40, httpx_kwargs=ipv4)
    app = (ApplicationBuilder().token(cfg.telegram_token)
           .request(request).get_updates_request(polling_request).build())
    app.bot_data.update(cfg=cfg, svc=svc)

    app.add_handler(CommandHandler(["start", "bantuan", "help"], cmd_start))
    app.add_handler(CommandHandler("hapus", _command("delete_command")))
    app.add_handler(CommandHandler("masuk", _command("income_command")))
    app.add_handler(CommandHandler("saldo", _command("saldo_command")))
    app.add_handler(CommandHandler("riwayat", _command("history_command")))
    app.add_handler(CommandHandler("edit", _command("edit_command")))
    app.add_handler(CommandHandler("ekspor", cmd_ekspor))
    app.add_handler(CommandHandler("tanya", _command("ask_command")))
    app.add_handler(CommandHandler("dashboard", _command("dashboard_command")))
    app.add_handler(CommandHandler("anggaran", _command("budget_command")))
    app.add_handler(CommandHandler("ringkasan", _command("summary_command")))
    app.add_handler(CommandHandler("tagihan", _command("bills_command", kind="tagihan")))
    app.add_handler(CommandHandler("langganan", _command("bills_command", kind="langganan")))
    app.add_handler(CommandHandler("pakai", _command("confirm_command")))
    app.add_handler(CommandHandler("batal", _command("cancel_command")))
    app.add_handler(CommandHandler("scan_email", cmd_scan_email))
    app.add_handler(CallbackQueryHandler(on_confirm, pattern=r"^(save|drop):"))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    tz = ZoneInfo(cfg.timezone)
    app.job_queue.run_daily(job_daily, time=dtime(8, 0, tzinfo=tz), name="harian")
    app.job_queue.run_daily(job_weekly, time=dtime(19, 0, tzinfo=tz), name="mingguan")
    if cfg.email_enabled:
        app.job_queue.run_repeating(job_email, interval=cfg.email_scan_hours * 3600,
                                    first=60, name="email")
    return app
