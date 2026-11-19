"""Adapter Telegram: hanya transport. Semua logika ada di service.py / access.py."""
from __future__ import annotations

import asyncio
import functools
import io
import logging
import uuid
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

from .backup import run_backup
from .config import Config
from .parser import ParsedExpense
from .service import Service

log = logging.getLogger(__name__)
MAX_PENDING = 20
MAX_PHOTO_BYTES = 8_000_000  # foto struk lebih besar dari ini ditolak (hemat memori/AI)
Ctx = ContextTypes.DEFAULT_TYPE


def _svc(context: Ctx) -> Service:
    return context.application.bot_data["svc"]


def _cfg(context: Ctx) -> Config:
    return context.application.bot_data["cfg"]


def _name(update: Update) -> str:
    user = update.effective_user
    return " ".join(p for p in (user.first_name, user.last_name) if p) if user else ""


def _private(update: Update) -> bool:
    """Hanya chat pribadi: di grup, data satu orang akan tampak oleh semua anggota."""
    chat = update.effective_chat
    return chat is not None and chat.type == "private"


async def _allowed(update: Update, context: Ctx) -> bool:
    if not _private(update):
        return False
    svc, chat = _svc(context), update.effective_chat
    state = await asyncio.to_thread(svc.access.state, chat.id, _name(update))
    if state == "ok":
        return True
    if update.effective_message:
        await update.effective_message.reply_text(svc.access.gate_message(state, chat.id))
    return False


async def _reply(update: Update, text: str, **kw) -> None:
    # Tanpa pratinjau tautan: Telegram tidak boleh "mengunjungi" tautan login dashboard.
    await update.effective_message.reply_text(
        text, link_preview_options=LinkPreviewOptions(is_disabled=True), **kw)


def _args(context: Ctx) -> str:
    return " ".join(context.args or [])


# ---------- pendaftaran & privasi (tidak butuh akun aktif) ----------
async def cmd_start(update: Update, context: Ctx) -> None:
    if not _private(update):
        return
    svc, chat = _svc(context), update.effective_chat
    state = await asyncio.to_thread(svc.access.state, chat.id, _name(update))
    if state == "ok":
        await _reply(update, svc.help_text(chat.id))
    elif state == "unregistered" and (context.args or svc.access.open_registration):
        # tautan undangan t.me/bot?start=KODE, atau pendaftaran terbuka: /start langsung mendaftar
        await _reply(update, await asyncio.to_thread(
            svc.access.register, chat.id, _name(update), _args(context)))
    else:
        await _reply(update, svc.access.gate_message(state, chat.id))


async def cmd_daftar(update: Update, context: Ctx) -> None:
    if _private(update):
        chat = update.effective_chat
        await _reply(update, await asyncio.to_thread(
            _svc(context).access.register, chat.id, _name(update), _args(context)))


async def cmd_setuju(update: Update, context: Ctx) -> None:
    if _private(update):
        await _reply(update, await asyncio.to_thread(
            _svc(context).access.consent, update.effective_chat.id))


async def cmd_privasi(update: Update, context: Ctx) -> None:
    if _private(update):
        await _reply(update, _svc(context).access.privacy())


async def cmd_hapus_akun(update: Update, context: Ctx) -> None:
    # Hak hapus data: boleh bagi siapa pun yang punya data, termasuk akun yang diblokir.
    if _private(update):
        await _reply(update, await asyncio.to_thread(
            _svc(context).access.delete_account, update.effective_chat.id, _args(context)))


# ---------- pesan biasa ----------
async def on_text(update: Update, context: Ctx) -> None:
    if not await _allowed(update, context):
        return
    text = update.effective_message.text or ""
    await update.effective_chat.send_action(ChatAction.TYPING)
    replied = update.effective_message.reply_to_message
    reply_to = replied.text if replied and replied.from_user and replied.from_user.is_bot else None
    reply = await asyncio.to_thread(_svc(context).handle_text, update.effective_chat.id, text,
                                    reply_to)
    await _reply(update, reply)


async def on_photo(update: Update, context: Ctx) -> None:
    if not await _allowed(update, context):
        return
    svc = _svc(context)
    photo = update.effective_message.photo[-1]
    if photo.file_size and photo.file_size > MAX_PHOTO_BYTES:
        await _reply(update, "Fotonya terlalu besar. Kirim foto yang lebih kecil atau ketik manual.")
        return
    if svc.llm:  # pembacaan struk oleh model lokal bisa 1-2 menit: beri tanda bot tidak diam
        await _reply(update, "🧾 Membaca struk… bisa 1–2 menit, tunggu ya.")
    await update.effective_chat.send_action(ChatAction.TYPING)
    file = await photo.get_file()
    data = bytes(await file.download_as_bytearray())
    parsed, message = await asyncio.to_thread(svc.read_receipt, update.effective_chat.id, data,
                                              "image/jpeg")
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


async def on_confirm(update: Update, context: Ctx) -> None:
    query = update.callback_query
    if query is None or not await _allowed(update, context):
        return
    await query.answer()
    action, _, token = (query.data or "").partition(":")
    parsed = context.chat_data.get("pending", {}).pop(token, None)  # chat_data terpisah per chat
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
    """Buat handler perintah yang meneruskan argumen ke Service.<method> (boleh 'access.invite')."""

    async def handler(update: Update, context: Ctx) -> None:
        if not await _allowed(update, context):
            return
        chat_id = update.effective_chat.id
        fn = functools.reduce(getattr, method.split("."), _svc(context))
        args = (chat_id, kind, _args(context)) if kind else (chat_id, _args(context))
        await update.effective_chat.send_action(ChatAction.TYPING)  # jawaban AI bisa lama
        await _reply(update, await asyncio.to_thread(fn, *args))

    return handler


async def cmd_ekspor(update: Update, context: Ctx) -> None:
    if not await _allowed(update, context):
        return
    result = await asyncio.to_thread(_svc(context).export_command, update.effective_chat.id,
                                     _args(context))
    if isinstance(result, str):
        await _reply(update, result)
        return
    filename, data = result
    caption = ("📦 Semua datamu (transaksi, anggaran, tagihan, target, utang, catatan rutin). "
               "Ekstrak lalu buka CSV dengan Excel/Google Sheets." if filename.endswith(".zip") else
               "📤 Buka dengan Excel/Google Sheets. Kolom: jenis, id, tanggal, jumlah, kategori, catatan, sumber.")
    await update.effective_message.reply_document(
        document=io.BytesIO(data), filename=filename, caption=caption)


async def cmd_scan_email(update: Update, context: Ctx) -> None:
    if not await _allowed(update, context):
        return
    await _reply(update, "Memeriksa email…")
    findings = await asyncio.to_thread(_svc(context).scan_email, update.effective_chat.id)
    await _reply(update, "\n".join(findings) if findings else "Tidak ada tagihan/langganan baru di email.")


# ---------- jadwal ----------
async def _send_all(context: Ctx, chat_id: int, texts: list[str]) -> None:
    """Kirim ke satu pengguna; kegagalan satu pengguna (mis. memblokir bot) tak menghentikan yang lain."""
    for text in texts:
        try:
            await context.bot.send_message(chat_id, text)
        except TelegramError as exc:
            log.warning("Gagal kirim ke %s: %s", chat_id, exc)
            return
        await asyncio.sleep(0.05)  # jauh di bawah batas laju Telegram


async def job_daily(context: Ctx) -> None:
    svc = _svc(context)
    for chat_id in svc.access.active_chat_ids():
        texts = await asyncio.to_thread(svc.daily_job, chat_id)
        await _send_all(context, chat_id, texts)


async def job_evening(context: Ctx) -> None:
    svc = _svc(context)
    for chat_id in svc.plans.evening_chat_ids() & set(svc.access.active_chat_ids()):
        texts = await asyncio.to_thread(svc.evening_job, chat_id)
        await _send_all(context, chat_id, texts)


async def job_weekly(context: Ctx) -> None:
    if datetime.now(ZoneInfo(_cfg(context).timezone)).weekday() != 6:  # hanya hari Minggu
        return
    svc = _svc(context)
    for chat_id in svc.access.active_chat_ids():
        text = await asyncio.to_thread(svc.weekly_job, chat_id)
        await _send_all(context, chat_id, [text])


async def job_email(context: Ctx) -> None:
    svc = _svc(context)
    for chat_id in sorted(svc.access.admin_ids):  # satu kotak surat: milik admin
        findings = await asyncio.to_thread(svc.scan_email, chat_id)
        await _send_all(context, chat_id, findings)


async def job_backup(context: Ctx) -> None:
    cfg = _cfg(context)
    svc = _svc(context)
    try:
        path = await asyncio.to_thread(run_backup, svc.db, cfg.backup_dir, cfg.backup_keep)
        log.info("Cadangan database: %s", path)
    except OSError as exc:
        log.error("Cadangan database GAGAL: %s", exc)
    await asyncio.to_thread(svc.access.accounts.purge_expired)  # sesi/token/hitungan AI kedaluwarsa


async def on_error(update: object, context: Ctx) -> None:
    log.exception("Error tak tertangani", exc_info=context.error)


async def _on_start(app: Application) -> None:
    me = await app.bot.get_me()
    app.bot_data["svc"].access.bot_username = me.username  # untuk tautan undangan


def build_app(cfg: Config, svc: Service) -> Application:
    # Jaringan rumahan sering lambat ke Telegram: timeout longgar + paksa IPv4 (IPv6 kadang macet).
    ipv4 = {"transport": httpx.AsyncHTTPTransport(local_address="0.0.0.0")}
    request = HTTPXRequest(connect_timeout=20, read_timeout=30, write_timeout=30,
                           pool_timeout=20, httpx_kwargs=ipv4)
    polling_request = HTTPXRequest(connect_timeout=20, read_timeout=40, httpx_kwargs=ipv4)
    app = (ApplicationBuilder().token(cfg.telegram_token)
           .request(request).get_updates_request(polling_request)
           .post_init(_on_start).build())
    app.bot_data.update(cfg=cfg, svc=svc)

    app.add_handler(CommandHandler(["start", "bantuan", "help"], cmd_start))
    app.add_handler(CommandHandler("daftar", cmd_daftar))
    app.add_handler(CommandHandler("setuju", cmd_setuju))
    app.add_handler(CommandHandler("privasi", cmd_privasi))
    app.add_handler(CommandHandler("hapus_akun", cmd_hapus_akun))
    # admin (dicek lagi di AccessControl: bukan admin -> "khusus admin")
    app.add_handler(CommandHandler("undang", _command("access.invite")))
    app.add_handler(CommandHandler("pengguna", _command("access.users")))
    app.add_handler(CommandHandler("blokir", _command("access.block")))
    app.add_handler(CommandHandler("aktifkan", _command("access.unblock")))
    app.add_handler(CommandHandler("statistik", _command("access.stats")))
    # fitur utama
    app.add_handler(CommandHandler("hapus", _command("delete_command")))
    app.add_handler(CommandHandler("masuk", _command("income_command")))
    app.add_handler(CommandHandler("saldo", _command("saldo_command")))
    app.add_handler(CommandHandler("riwayat", _command("history_command")))
    app.add_handler(CommandHandler("edit", _command("edit_command")))
    app.add_handler(CommandHandler("ekspor", cmd_ekspor))
    app.add_handler(CommandHandler("tanya", _command("ask_command")))
    app.add_handler(CommandHandler("dashboard", _command("dashboard_command")))
    app.add_handler(CommandHandler("aman", _command("aman_command")))
    app.add_handler(CommandHandler("gajian", _command("gajian_command")))
    app.add_handler(CommandHandler("target", _command("target_command")))
    app.add_handler(CommandHandler("tabung", _command("tabung_command")))
    app.add_handler(CommandHandler("tarik", _command("tarik_command")))
    app.add_handler(CommandHandler("utang", _command("utang_command")))
    app.add_handler(CommandHandler("piutang", _command("piutang_command")))
    app.add_handler(CommandHandler("lunas", _command("lunas_command")))
    app.add_handler(CommandHandler("tagih", _command("tagih_command")))
    app.add_handler(CommandHandler("patungan", _command("patungan_command")))
    app.add_handler(CommandHandler("anggaran", _command("budget_command")))
    app.add_handler(CommandHandler("ringkasan", _command("summary_command")))
    app.add_handler(CommandHandler("tagihan", _command("bills_command", kind="tagihan")))
    app.add_handler(CommandHandler("langganan", _command("bills_command", kind="langganan")))
    app.add_handler(CommandHandler("pakai", _command("confirm_command")))
    app.add_handler(CommandHandler("batal", _command("cancel_command")))
    app.add_handler(CommandHandler("insight", _command("insight_command")))
    app.add_handler(CommandHandler("rutin",_command("rutin_command")))
    app.add_handler(CommandHandler("ingatkan", _command("ingatkan_command")))
    app.add_handler(CommandHandler("scan_email", cmd_scan_email))
    app.add_handler(CallbackQueryHandler(on_confirm, pattern=r"^(save|drop):"))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    tz = ZoneInfo(cfg.timezone)
    app.job_queue.run_daily(job_daily, time=dtime(8, 0, tzinfo=tz), name="harian")
    app.job_queue.run_daily(job_weekly, time=dtime(19, 0, tzinfo=tz), name="mingguan")
    app.job_queue.run_daily(job_evening, time=dtime(20, 30, tzinfo=tz), name="malam")
    # Laptop bisa mati pukul 08:00: jalankan sekali tak lama setelah bot menyala (aman diulang).
    app.job_queue.run_once(job_daily, when=90, name="harian-awal")
    app.job_queue.run_daily(job_backup, time=dtime(3, 30, tzinfo=tz), name="cadangan")
    if cfg.email_enabled:
        app.job_queue.run_repeating(job_email, interval=cfg.email_scan_hours * 3600,
                                    first=60, name="email")
    return app
