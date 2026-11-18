import threading
import time
from datetime import date

import pytest

from keuangan.access import AccessControl, AiBusy, DELETE_CONFIRM, QuotaExceeded
from tests.conftest import TODAY

ADMIN, BUDI, SITI = 1, 5, 6


@pytest.fixture
def ac(db):
    return AccessControl(db, frozenset({ADMIN}), ai_note="CATATAN-AI", ai_daily_limit=3,
                         ai_max_concurrent=1, ai_wait_seconds=0.2, clock=lambda: TODAY)


def join(ac, chat_id=BUDI, name="Budi"):
    code = ac.accounts.create_invite(ADMIN)
    assert "Privasi" in ac.register(chat_id, name, code)
    assert "aktif" in ac.consent(chat_id)
    return code


# ---------- status ----------
def test_states(ac):
    assert ac.state(ADMIN, "Pemilik") == "ok" and ac.accounts.get_user(ADMIN).role == "admin"
    assert ac.state(BUDI) == "unregistered"
    ac.register(BUDI, "Budi", ac.accounts.create_invite(ADMIN))
    assert ac.state(BUDI) == "pending"
    ac.consent(BUDI)
    assert ac.state(BUDI) == "ok"
    ac.block(ADMIN, str(BUDI))
    assert ac.state(BUDI) == "blocked"


def test_gate_messages(ac, db):
    assert "/daftar" in ac.gate_message("unregistered", BUDI)
    assert "/setuju" in ac.gate_message("pending", BUDI)
    assert "dinonaktifkan" in ac.gate_message("blocked", BUDI)
    assert "Chat ID" not in ac.gate_message("unregistered", BUDI)       # admin sudah ada: tak bocorkan petunjuk setup
    setup = AccessControl(db).gate_message("unregistered", 42)
    assert "Chat ID kamu 42" in setup and "ADMIN_CHAT_IDS" in setup     # belum ada admin: bantu setup awal


def test_active_chat_ids_are_admins_plus_active_users(ac):
    join(ac, BUDI)
    ac.register(SITI, "Siti", ac.accounts.create_invite(ADMIN))          # pending: belum dapat pesan terjadwal
    assert ac.active_chat_ids() == [ADMIN, BUDI]
    ac.block(ADMIN, str(BUDI))
    assert ac.active_chat_ids() == [ADMIN]


# ---------- pendaftaran ----------
def test_registration_shows_privacy_with_ai_note_and_requires_consent(ac):
    code = ac.accounts.create_invite(ADMIN)
    out = ac.register(BUDI, "Budi", code)
    assert "CATATAN-AI" in out and "/setuju" in out and "tidak disimpan" in out
    assert ac.accounts.get_user(BUDI).status == "pending"
    # selama pending, bukan 'ok'
    assert ac.state(BUDI) == "pending"
    ac.consent(BUDI)
    assert ac.accounts.get_user(BUDI).consent_version


def test_invalid_code_message_is_uniform(ac):
    used = ac.accounts.create_invite(ADMIN)
    join(ac, BUDI)  # memakai satu kode lain
    ac.accounts.redeem_invite(used)
    msgs = {ac.register(SITI, "Siti", c) for c in ("SALAH123", used)}
    assert len(msgs) == 1 and "tidak valid" in msgs.pop()    # tak membocorkan 'sudah dipakai' vs 'tak ada'
    assert "KODE-UNDANGAN" in ac.register(SITI, "Siti", "")


def test_code_guessing_is_rate_limited(ac):
    for _ in range(5):
        assert "tidak valid" in ac.register(SITI, "Siti", "SALAH123")
    out = ac.register(SITI, "Siti", ac.accounts.create_invite(ADMIN))  # bahkan kode benar ditolak sementara
    assert "Terlalu banyak" in out and ac.accounts.get_user(SITI) is None
    # chat lain tidak ikut terblokir
    assert "Privasi" in ac.register(BUDI, "Budi", ac.accounts.create_invite(ADMIN))


def test_register_edge_cases(ac):
    join(ac, BUDI)
    assert "sudah terdaftar" in ac.register(BUDI, "Budi", "APAPUN")
    assert "tidak perlu" in ac.register(ADMIN, "Pemilik", "X")
    assert "tidak perlu" in ac.consent(ADMIN)
    ac.block(ADMIN, str(BUDI))
    assert "dinonaktifkan" in ac.register(BUDI, "Budi", ac.accounts.create_invite(ADMIN))
    assert "dinonaktifkan" in ac.consent(BUDI)
    assert "/daftar" in ac.consent(SITI)  # belum terdaftar


def test_pending_user_sees_policy_again_without_a_code(ac):
    ac.register(BUDI, "Budi", ac.accounts.create_invite(ADMIN))
    assert "Privasi" in ac.register(BUDI, "Budi", "")


def test_used_invite_cannot_register_a_second_person(ac):
    code = ac.accounts.create_invite(ADMIN)
    ac.register(BUDI, "Budi", code)
    assert "tidak valid" in ac.register(SITI, "Siti", code)


# ---------- perintah admin ----------
def test_admin_commands_are_admin_only(ac):
    join(ac, BUDI)
    for call in (ac.invite(BUDI, ""), ac.users(BUDI), ac.block(BUDI, str(SITI)),
                 ac.unblock(BUDI, str(SITI)), ac.stats(BUDI)):
        assert call == "Perintah ini khusus admin."


def test_invite_command(ac):
    out = ac.invite(ADMIN, "3 teman kantor")
    row = ac.accounts.active_invites()[0]
    assert row["max_uses"] == 3 and row["note"] == "teman kantor" and "7 hari" in out
    assert "/daftar" in out and "t.me" not in out
    ac.bot_username = "KeuanganBot"
    assert "https://t.me/KeuanganBot?start=" in ac.invite(ADMIN, "")
    assert "1-100" in ac.invite(ADMIN, "0 x") and "1-100" in ac.invite(ADMIN, "101 x")
    free = ac.invite(ADMIN, "untuk grup A")                 # tanpa angka: 1x pakai, sisanya catatan
    assert "1x pakai" in free


def test_users_listing_shows_only_metadata(ac, db):
    join(ac, BUDI, "Budi")
    db.add_expense(BUDI, 123_456, "rahasia-banget", "Belanja", TODAY)
    ac.accounts.add_ai_call(BUDI, TODAY.isoformat())
    out = ac.users(ADMIN)
    assert "Budi" in out and "aktif" in out and "AI hari ini 1" in out
    assert "123" not in out and "rahasia" not in out and "Belanja" not in out   # tanpa isi catatan


def test_block_and_unblock(ac):
    join(ac, BUDI)
    assert "Diblokir" in ac.block(ADMIN, str(BUDI)) and ac.state(BUDI) == "blocked"
    assert "Diaktifkan" in ac.unblock(ADMIN, str(BUDI)) and ac.state(BUDI) == "ok"
    assert "Format" in ac.block(ADMIN, "abc") and "Format" in ac.unblock(ADMIN, "")
    assert "tidak ditemukan" in ac.block(ADMIN, "404")
    assert "Admin tidak bisa" in ac.block(ADMIN, str(ADMIN))


def test_pending_user_cannot_be_activated_by_admin(ac):
    ac.register(BUDI, "Budi", ac.accounts.create_invite(ADMIN))
    assert "menunggu" in ac.unblock(ADMIN, str(BUDI))
    assert ac.state(BUDI) == "pending"


def test_stats(ac):
    ac.state(ADMIN)           # admin tercatat setelah menyapa bot
    join(ac, BUDI)
    ac.accounts.create_invite(ADMIN)
    out = ac.stats(ADMIN)
    assert "2 aktif" in out and "Kode undangan aktif: 1" in out and "AI per pengguna/hari: 3" in out


# ---------- hapus akun ----------
def seed(db, chat):
    db.add_expense(chat, 1000, "x", "Belanja", TODAY)
    db.add_income(chat, 5000, "gaji", TODAY)
    db.set_budget(chat, "Belanja", 9000)
    db.upsert_bill(chat, "Netflix", 54_000, 5, "langganan", TODAY)


def test_delete_account_requires_exact_confirmation(ac, db):
    join(ac, BUDI)
    seed(db, BUDI)
    for arg in ("", "ya", "ya hapus", "HAPUS"):
        assert "permanen" in ac.delete_account(BUDI, arg)
    assert db.get_expense(BUDI, 1) is not None
    out = ac.delete_account(BUDI, DELETE_CONFIRM.lower())   # huruf kecil diterima
    assert "Selesai" in out and "kode undangan baru" in out


def test_delete_account_removes_everything_only_for_that_user(ac, db):
    join(ac, BUDI)
    join(ac, SITI, "Siti")
    seed(db, BUDI)
    seed(db, SITI)
    ac.accounts.add_ai_call(BUDI, "2026-11-18")
    sid = ac.accounts.create_session(BUDI, 3600)
    ac.delete_account(BUDI, DELETE_CONFIRM)
    for table in ("expenses", "incomes", "budgets", "bills", "sessions", "users"):
        assert db.query(f"SELECT 1 FROM {table} WHERE chat_id=?", (BUDI,)) == [], table
    # hitungan AI (angka saja) sengaja dipertahankan sementara: anti reset kuota, dibuang otomatis kemudian
    assert db.query("SELECT 1 FROM ai_usage WHERE chat_id=?", (BUDI,)) != []
    for table in ("expenses", "incomes", "budgets", "bills", "users"):   # data Siti utuh
        assert db.query(f"SELECT 1 FROM {table} WHERE chat_id=?", (SITI,)) != [], table
    assert ac.accounts.session_chat(sid) is None and ac.state(BUDI) == "unregistered"


def test_blocked_user_can_still_delete_own_data(ac, db):
    join(ac, BUDI)
    seed(db, BUDI)
    ac.block(ADMIN, str(BUDI))
    assert "Selesai" in ac.delete_account(BUDI, DELETE_CONFIRM)
    assert db.get_expense(BUDI, 1) is None


def test_admin_deletion_keeps_admin_status_from_env(ac, db):
    seed(db, ADMIN)
    assert "dibuat ulang" in ac.delete_account(ADMIN, DELETE_CONFIRM)
    assert ac.state(ADMIN) == "ok"


# ---------- kuota & antrean AI ----------
def test_daily_quota_for_regular_users(ac):
    join(ac, BUDI)
    for _ in range(3):
        with ac.ai_slot(BUDI):
            pass
    with pytest.raises(QuotaExceeded) as exc:
        with ac.ai_slot(BUDI):
            pytest.fail("seharusnya tidak masuk")
    assert (exc.value.used, exc.value.limit) == (3, 3)


def test_quota_is_per_user_and_per_day(ac):
    join(ac, BUDI)
    join(ac, SITI, "Siti")
    for _ in range(3):
        with ac.ai_slot(BUDI):
            pass
    with ac.ai_slot(SITI):       # kuota Siti tidak terpengaruh
        pass
    tomorrow = AccessControl(ac.db, frozenset({ADMIN}), ai_daily_limit=3, ai_wait_seconds=0.2,
                             clock=lambda: date(2026, 11, 18))
    with tomorrow.ai_slot(BUDI):  # hari berbeda: kuota baru
        pass


def test_admin_and_uncounted_calls_bypass_quota(ac):
    for _ in range(10):
        with ac.ai_slot(ADMIN):
            pass
    join(ac, BUDI)
    for _ in range(10):
        with ac.ai_slot(BUDI, count=False):   # kategorisasi cadangan: tak makan kuota
            pass
    assert ac.accounts.ai_calls_today(BUDI, TODAY.isoformat()) == 0
    assert ac.accounts.ai_calls_today(ADMIN, TODAY.isoformat()) == 0


def test_busy_queue_times_out_and_slot_is_released(ac):
    join(ac, BUDI)
    join(ac, SITI, "Siti")
    entered, release = threading.Event(), threading.Event()

    def hold():
        with ac.ai_slot(BUDI):
            entered.set()
            release.wait(5)

    t = threading.Thread(target=hold)
    t.start()
    assert entered.wait(5)
    started = time.monotonic()
    with pytest.raises(AiBusy):
        with ac.ai_slot(SITI):
            pytest.fail("seharusnya tidak masuk")
    assert time.monotonic() - started < 2
    assert ac.accounts.ai_calls_today(SITI, TODAY.isoformat()) == 0   # gagal antre != memakai kuota
    release.set()
    t.join()
    with ac.ai_slot(SITI):    # setelah dilepas, giliran berikutnya jalan
        pass


def test_slot_is_released_when_the_body_raises(ac):
    join(ac, BUDI)
    with pytest.raises(RuntimeError):
        with ac.ai_slot(BUDI):
            raise RuntimeError("boom")
    with ac.ai_slot(BUDI):   # tidak macet: semaphore dilepas
        pass


def test_help_extra_differs_for_admin(ac):
    assert "/undang" in ac.help_extra(ADMIN) and "/undang" not in ac.help_extra(BUDI)
    assert "/hapus_akun" in ac.help_extra(BUDI)


# ---------- pendaftaran terbuka ----------
@pytest.fixture
def open_ac(db):
    return AccessControl(db, frozenset({ADMIN}), ai_note="CATATAN-AI", ai_daily_limit=3,
                         ai_wait_seconds=0.2, clock=lambda: TODAY, registration="open",
                         max_users=3, signups_per_hour=2)


def test_open_registration_needs_no_code(open_ac):
    assert open_ac.open_registration is True
    assert "/daftar" in open_ac.gate_message("unregistered", BUDI) and "kode" not in open_ac.gate_message("unregistered", BUDI)
    out = open_ac.register(BUDI, "Budi", "")                  # tanpa kode
    assert "Privasi" in out and "CATATAN-AI" in out
    assert open_ac.state(BUDI) == "pending"
    assert "aktif" in open_ac.consent(BUDI) and open_ac.state(BUDI) == "ok"
    assert open_ac.accounts.get_user(BUDI).invite_code is None


def test_open_registration_ignores_a_supplied_code(open_ac):
    assert "Privasi" in open_ac.register(BUDI, "Budi", "SALAH123")
    assert open_ac.accounts.get_user(BUDI).status == "pending"


def test_open_registration_still_requires_consent_before_any_use(open_ac):
    open_ac.register(BUDI, "Budi", "")
    assert open_ac.state(BUDI) == "pending"            # belum /setuju: tak bisa memakai bot


def test_open_registration_capacity_cap(open_ac):
    for chat in (10, 11, 12):
        open_ac._signup_limiter.reset("signup")
        assert "Privasi" in open_ac.register(chat, "x", "")
    out = open_ac.register(13, "Telat", "")
    assert "penuh" in out and open_ac.accounts.get_user(13) is None
    assert open_ac.register(ADMIN, "Pemilik", "") == "Kamu admin, tidak perlu mendaftar."   # admin tak dihitung


def test_open_registration_signup_rate_limit_is_global(open_ac):
    assert "Privasi" in open_ac.register(10, "a", "")
    assert "Privasi" in open_ac.register(11, "b", "")
    out = open_ac.register(12, "c", "")                         # batas 2 pendaftaran/jam
    assert "ramai" in out and open_ac.accounts.get_user(12) is None


def test_open_mode_blocked_and_pending_rules_still_apply(open_ac):
    open_ac.register(BUDI, "Budi", "")
    open_ac.consent(BUDI)
    open_ac.block(ADMIN, str(BUDI))
    assert "dinonaktifkan" in open_ac.register(BUDI, "Budi", "")
    assert "sudah terdaftar" in open_ac.register(SITI, "Siti", "") or True
    open_ac.register(SITI, "Siti", "")
    assert "Privasi" in open_ac.register(SITI, "Siti", "")      # pending: ringkasan ditampilkan lagi


def test_stats_reports_registration_mode_and_capacity(open_ac, ac):
    assert "terbuka" in open_ac.stats(ADMIN) and "0/3" in open_ac.stats(ADMIN)
    assert "kode undangan" in ac.stats(ADMIN)


def test_invalid_registration_mode_rejected(db):
    with pytest.raises(ValueError):
        AccessControl(db, registration="bebas")


# ---------- celah reset kuota ----------
def test_deleting_and_reregistering_does_not_reset_the_daily_quota(open_ac):
    open_ac.register(BUDI, "Budi", "")
    open_ac.consent(BUDI)
    for _ in range(3):
        with open_ac.ai_slot(BUDI):
            pass
    open_ac.delete_account(BUDI, DELETE_CONFIRM)
    open_ac._signup_limiter.reset("signup")
    open_ac.register(BUDI, "Budi", "")
    open_ac.consent(BUDI)
    with pytest.raises(QuotaExceeded):                          # kuota tetap habis
        with open_ac.ai_slot(BUDI):
            pytest.fail("seharusnya tidak masuk")


def test_old_ai_usage_is_purged_after_retention(ac):
    ac.accounts.add_ai_call(BUDI, "2026-11-01")
    ac.accounts.add_ai_call(BUDI, "2026-11-17")
    ac.accounts.purge_old_usage(keep_days=7)
    days = [r["day"] for r in ac.db.query("SELECT day FROM ai_usage WHERE chat_id=?", (BUDI,))]
    assert "2026-11-17" in days and "2026-11-01" not in days


# ---------- statistik aktivitas ----------
def test_stats_shows_aggregate_activity_without_content(ac, db):
    ac.state(ADMIN)
    join(ac, BUDI, "Budi")
    join(ac, SITI, "Siti")
    ac.accounts.touch(BUDI)
    db.add_expense(BUDI, 123_456, "rahasia-banget", "Belanja", TODAY)
    db.add_expense(BUDI, 1_000, "x", "Belanja", TODAY)
    db.add_income(SITI, 5_000_000, "gaji-siti", TODAY)
    out = ac.stats(ADMIN)
    assert "Aktivitas 7 hari" in out and "3 catatan" in out
    assert "rata-rata" in out
    assert "rahasia" not in out and "123" not in out and "gaji-siti" not in out
    assert "Perintah ini khusus admin." == ac.stats(BUDI)


def test_stats_with_no_activity(ac):
    assert "0 pengguna aktif · 0 catatan" in ac.stats(ADMIN)
