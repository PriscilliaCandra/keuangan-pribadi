"""Gerbang akses di adapter Telegram, dengan objek Update palsu (tanpa jaringan)."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import Forbidden

from keuangan import bot
from keuangan.access import AccessControl
from keuangan.config import load_config
from keuangan.service import Service
from tests.conftest import TODAY

ADMIN, BUDI, SITI = 1, 5, 6


@pytest.fixture
def svc(db):
    access = AccessControl(db, frozenset({ADMIN}), ai_daily_limit=5, ai_wait_seconds=0.2,
                           clock=lambda: TODAY)
    return Service(db, None, clock=lambda: TODAY, access=access)


def context(svc, args=None, chat_data=None):
    cfg = load_config({"TELEGRAM_BOT_TOKEN": "1:T", "ADMIN_CHAT_IDS": str(ADMIN)})
    sent = []
    send = AsyncMock(side_effect=lambda chat_id, text, **kw: sent.append((chat_id, text)))
    return SimpleNamespace(application=SimpleNamespace(bot_data={"svc": svc, "cfg": cfg}),
                           args=args or [], chat_data=chat_data if chat_data is not None else {},
                           bot=SimpleNamespace(send_message=send), sent=sent)


def update(chat_id, text="", chat_type="private", name="Budi", photo=None, reply_to=None):
    msg = SimpleNamespace(reply_text=AsyncMock(), text=text, photo=photo or [], reply_to_message=reply_to,
                          reply_document=AsyncMock())
    chat = SimpleNamespace(id=chat_id, type=chat_type, send_action=AsyncMock())
    return SimpleNamespace(effective_chat=chat, effective_message=msg, callback_query=None,
                           effective_user=SimpleNamespace(first_name=name, last_name=None))


def replies(u):
    return [c.args[0] for c in u.effective_message.reply_text.call_args_list]


def run(coro):
    return asyncio.run(coro)


def register(svc, chat_id, name="Budi"):
    code = svc.access.accounts.create_invite(ADMIN)
    run(bot.cmd_daftar(update(chat_id, name=name), context(svc, [code])))
    run(bot.cmd_setuju(update(chat_id, name=name), context(svc)))


# ---------- gerbang ----------
def test_groups_and_channels_are_ignored_completely(svc, db):
    for kind in ("group", "supergroup", "channel"):
        u = update(ADMIN, "kopi 25rb", chat_type=kind)          # bahkan admin: data tak boleh tampil di grup
        run(bot.on_text(u, context(svc)))
        run(bot.cmd_start(u, context(svc)))
        run(bot.cmd_daftar(u, context(svc, ["ABCD2345"])))
        assert u.effective_message.reply_text.await_count == 0
    assert db.query("SELECT 1 FROM expenses") == []


def test_unregistered_user_gets_gate_message_and_nothing_is_recorded(svc, db):
    u = update(BUDI, "kopi 25rb")
    run(bot.on_text(u, context(svc)))
    assert "/daftar" in replies(u)[0]
    assert db.query("SELECT 1 FROM expenses") == []
    u2 = update(BUDI)
    run(bot.cmd_scan_email(u2, context(svc)))
    assert "/daftar" in replies(u2)[0]


def test_registration_flow_through_the_bot(svc, db):
    code = svc.access.accounts.create_invite(ADMIN)
    u = update(BUDI)
    run(bot.cmd_daftar(u, context(svc, [code])))
    assert "Privasi" in replies(u)[0]
    u = update(BUDI, "kopi 25rb")
    run(bot.on_text(u, context(svc)))                          # masih pending
    assert "/setuju" in replies(u)[0] and db.query("SELECT 1 FROM expenses") == []
    u = update(BUDI)
    run(bot.cmd_setuju(u, context(svc)))
    assert "aktif" in replies(u)[0]
    u = update(BUDI, "kopi 25rb")
    run(bot.on_text(u, context(svc)))
    assert "Dicatat" in replies(u)[0] and len(db.query("SELECT 1 FROM expenses WHERE chat_id=?", (BUDI,))) == 1


def test_start_with_invite_code_deep_link_registers(svc):
    code = svc.access.accounts.create_invite(ADMIN)
    u = update(BUDI)
    run(bot.cmd_start(u, context(svc, [code])))
    assert "Privasi" in replies(u)[0]
    u = update(SITI)
    run(bot.cmd_start(u, context(svc)))                        # tanpa kode: petunjuk beta tertutup
    assert "beta tertutup" in replies(u)[0]


def test_start_shows_help_for_active_user_with_admin_extras_only_for_admin(svc):
    register(svc, BUDI)
    u = update(BUDI)
    run(bot.cmd_start(u, context(svc)))
    assert "Aku pencatat" in replies(u)[0] and "/undang" not in replies(u)[0]
    a = update(ADMIN, name="Pemilik")
    run(bot.cmd_start(a, context(svc)))
    assert "/undang" in replies(a)[0]


def test_blocked_user_is_denied_but_can_still_delete_data(svc, db):
    register(svc, BUDI)
    run(bot.on_text(update(BUDI, "kopi 25rb"), context(svc)))
    svc.access.block(ADMIN, str(BUDI))
    u = update(BUDI, "kopi 30rb")
    run(bot.on_text(u, context(svc)))
    assert "dinonaktifkan" in replies(u)[0]
    d = update(BUDI)
    run(bot.cmd_hapus_akun(d, context(svc, ["YA-HAPUS"])))
    assert "Selesai" in replies(d)[0] and db.query("SELECT 1 FROM expenses") == []


def test_admin_commands_denied_for_regular_users_via_the_bot(svc):
    register(svc, BUDI)
    u = update(BUDI)
    run(bot._command("access.invite")(u, context(svc)))
    assert replies(u)[0] == "Perintah ini khusus admin."
    a = update(ADMIN, name="Pemilik")
    run(bot._command("access.invite")(a, context(svc)))
    assert "Kode undangan" in replies(a)[0]


def test_replies_disable_link_previews(svc):
    """Telegram tidak boleh 'mengunjungi' tautan login dashboard untuk pratinjau."""
    register(svc, BUDI)
    svc.dashboard_base_url = "https://uang.contoh.id"
    u = update(BUDI)
    run(bot._command("dashboard_command")(u, context(svc)))
    kwargs = u.effective_message.reply_text.call_args.kwargs
    assert kwargs["link_preview_options"].is_disabled is True
    assert "https://uang.contoh.id/?t=" in replies(u)[0]


def test_oversized_photo_is_rejected_before_any_download(svc):
    register(svc, BUDI)
    photo = SimpleNamespace(file_size=bot.MAX_PHOTO_BYTES + 1, get_file=AsyncMock())
    u = update(BUDI, photo=[photo])
    run(bot.on_photo(u, context(svc)))
    assert "terlalu besar" in replies(u)[0]
    photo.get_file.assert_not_called()


def test_photo_from_unregistered_user_is_not_processed(svc):
    photo = SimpleNamespace(file_size=1000, get_file=AsyncMock())
    u = update(BUDI, photo=[photo])
    run(bot.on_photo(u, context(svc)))
    assert "/daftar" in replies(u)[0]
    photo.get_file.assert_not_called()


def test_privacy_is_public_but_requires_private_chat(svc):
    u = update(BUDI)
    run(bot.cmd_privasi(u, context(svc)))
    assert "Privasi" in replies(u)[0] and "/setuju" not in replies(u)[0]
    g = update(BUDI, chat_type="group")
    run(bot.cmd_privasi(g, context(svc)))
    assert g.effective_message.reply_text.await_count == 0


# ---------- dua pengguna tidak saling melihat ----------
def test_two_users_are_fully_isolated_through_the_bot(svc, db):
    register(svc, BUDI, "Budi")
    register(svc, SITI, "Siti")
    run(bot.on_text(update(BUDI, "kopi rahasia budi 25rb"), context(svc)))
    run(bot.on_text(update(BUDI, "gaji 8jt"), context(svc)))
    run(bot.on_text(update(SITI, "bensin 50rb"), context(svc)))
    for command in ("history_command", "saldo_command", "summary_command", "bills_command"):
        u = update(SITI)
        args = ("tagihan", "") if command == "bills_command" else ("",)
        run(bot._command(command, kind="tagihan" if command == "bills_command" else None)(u, context(svc)))
        text = " ".join(replies(u))
        assert "budi" not in text.lower() and "25.000" not in text and "8.000.000" not in text, command
    u = update(SITI)
    run(bot.cmd_ekspor(u, context(svc)))
    csv_bytes = u.effective_message.reply_document.call_args.kwargs["document"].read().decode("utf-8-sig")
    assert "bensin" in csv_bytes and "budi" not in csv_bytes.lower() and "gaji" not in csv_bytes
    # Siti tidak bisa menghapus/mengubah catatan Budi dengan menebak ID
    u = update(SITI)
    run(bot._command("delete_command")(u, context(svc, ["1"])))
    assert "tidak ditemukan" in replies(u)[0]
    assert db.get_expense(BUDI, 1) is not None
    # dan balasan (reply) ke pesan bot milik Budi tidak mengubah catatan Budi
    forged = SimpleNamespace(text="✅ Dicatat: Rp25.000 (ID 1)", from_user=SimpleNamespace(is_bot=True))
    u = update(SITI, "transport", reply_to=forged)
    run(bot.on_text(u, context(svc)))
    assert db.get_expense(BUDI, 1).category != "Transportasi"


def test_admin_cannot_read_users_records_via_commands(svc, db):
    register(svc, BUDI)
    run(bot.on_text(update(BUDI, "kopi rahasia 25rb"), context(svc)))
    for command in ("users", "stats"):
        u = update(ADMIN, name="Pemilik")
        run(bot._command(f"access.{command}")(u, context(svc)))
        assert "rahasia" not in replies(u)[0] and "25.000" not in replies(u)[0]
    u = update(ADMIN, name="Pemilik")
    run(bot._command("history_command")(u, context(svc)))         # riwayat milik admin sendiri
    assert "rahasia" not in replies(u)[0]


# ---------- jadwal ----------
def test_scheduled_jobs_reach_active_users_only_and_survive_one_failure(svc, db):
    register(svc, BUDI)
    register(svc, SITI, "Siti")
    svc.access.accounts.create_pending(9, "Pending", "CODE2345")            # belum setuju
    svc.access.accounts.create_pending(10, "Dodi", "CODE2345")
    svc.access.accounts.give_consent(10, "v1")
    svc.access.block(ADMIN, "10")
    for chat in (ADMIN, BUDI, SITI, 9, 10):
        db.upsert_bill(chat, "Listrik", 350_000, TODAY.day + 2 if TODAY.day <= 28 else 1, "tagihan", TODAY)
    ctx = context(svc)
    original = ctx.bot.send_message.side_effect

    async def flaky(chat_id, text, **kw):
        if chat_id == BUDI:
            raise Forbidden("bot diblokir pengguna")
        return original(chat_id, text, **kw)

    ctx.bot.send_message = AsyncMock(side_effect=flaky)
    run(bot.job_daily(ctx))
    recipients = {chat for chat, _ in ctx.sent}
    assert recipients == {ADMIN, SITI}            # Budi gagal, tapi tak menghentikan yang lain; pending & blocked tak dikirimi


def test_email_job_is_for_admins_only(svc):
    register(svc, BUDI)
    ctx = context(svc)
    run(bot.job_email(ctx))
    assert {chat for chat, _ in ctx.sent} <= {ADMIN}


def test_build_app_registers_new_commands(db, svc):
    cfg = load_config({"TELEGRAM_BOT_TOKEN": "123456:TEST", "ADMIN_CHAT_IDS": "1"})
    app = bot.build_app(cfg, svc)
    commands = {c for h in app.handlers[0] for c in getattr(h, "commands", [])}
    assert {"daftar", "setuju", "privasi", "hapus_akun", "undang", "pengguna", "blokir", "aktifkan",
            "statistik"} <= commands
    assert {j.name for j in app.job_queue.jobs()} >= {"harian", "mingguan", "cadangan"}
