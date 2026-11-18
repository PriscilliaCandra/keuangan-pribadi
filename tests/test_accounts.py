import threading
from datetime import datetime, timedelta, timezone

import pytest

from keuangan.accounts import (AccountStore, CODE_ALPHABET, clean_name, format_code, hash_token,
                               normalize_code)


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def acc(db, clock):
    return AccountStore(db, now=clock)


def make_active(acc, chat_id=5, name="Budi"):
    acc.create_pending(chat_id, name, "ABCD2345")
    assert acc.give_consent(chat_id, "v1")
    return acc.get_user(chat_id)


# ---------- helper ----------
def test_normalize_and_format_code():
    assert normalize_code(" k7m2-qx9r ") == "K7M2QX9R"
    assert normalize_code("") == "" and normalize_code(None) == ""
    assert format_code("K7M2QX9R") == "K7M2-QX9R"


def test_clean_name_strips_control_chars_and_limits_length():
    assert clean_name("Bu\x00di\n\t") == "Budi"
    assert len(clean_name("x" * 200)) == 60 and clean_name(None) == ""


# ---------- undangan ----------
def test_invite_code_shape(acc):
    code = acc.create_invite(1)
    assert len(code) == 8 and set(code) <= set(CODE_ALPHABET)
    assert not set(code) & set("01OIL")  # huruf/angka yang membingungkan tidak dipakai


def test_invite_is_single_use_by_default(acc):
    code = acc.create_invite(1)
    assert acc.redeem_invite(code) is True
    assert acc.redeem_invite(code) is False


def test_invite_multi_use_and_input_normalization(acc):
    code = acc.create_invite(1, max_uses=2)
    assert acc.redeem_invite(format_code(code).lower()) is True   # huruf kecil + tanda hubung
    assert acc.redeem_invite(code) is True
    assert acc.redeem_invite(code) is False


def test_invite_expires(acc, clock):
    code = acc.create_invite(1, ttl_days=7)
    clock.advance(days=8)
    assert acc.redeem_invite(code) is False
    never = acc.create_invite(1, ttl_days=None)
    clock.advance(days=3650)
    assert acc.redeem_invite(never) is True


@pytest.mark.parametrize("bad", ["", "ABC", "ZZZZZZZZ", "A" * 20, "'; DROP TABLE users;--", None])
def test_invalid_codes_rejected(acc, bad):
    assert acc.redeem_invite(bad) is False


@pytest.mark.parametrize("uses", [0, 101, -1])
def test_invite_use_limit_validation(acc, uses):
    with pytest.raises(ValueError):
        acc.create_invite(1, max_uses=uses)


def test_last_invite_slot_is_atomic_under_concurrency(acc):
    code = acc.create_invite(1)
    results = []

    def attempt():
        results.append(acc.redeem_invite(code))

    threads = [threading.Thread(target=attempt) for _ in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(True) == 1


def test_active_invites_excludes_used_and_expired(acc, clock):
    keep = acc.create_invite(1, note="a")
    used = acc.create_invite(1)
    acc.redeem_invite(used)
    old = acc.create_invite(1, ttl_days=1)
    clock.advance(days=2)
    assert [r["code"] for r in acc.active_invites()] == [keep] or keep in [r["code"] for r in acc.active_invites()]
    assert used not in [r["code"] for r in acc.active_invites()]
    assert old not in [r["code"] for r in acc.active_invites()]


# ---------- pengguna ----------
def test_pending_then_consent_activates(acc):
    u = acc.create_pending(5, "Budi", "CODE2345")
    assert u.status == "pending" and u.role == "user" and u.consent_at is None
    assert acc.give_consent(5, "v1") is True
    u = acc.get_user(5)
    assert u.status == "active" and u.consent_version == "v1" and u.consent_at
    assert acc.give_consent(5, "v2") is False  # sudah aktif: tidak menimpa catatan persetujuan awal
    assert acc.get_user(5).consent_version == "v1"


def test_create_pending_does_not_overwrite_existing(acc):
    make_active(acc)
    acc.create_pending(5, "Orang Lain", "ZZZZ2345")
    assert acc.get_user(5).status == "active" and acc.get_user(5).display_name == "Budi"


def test_ensure_admin_is_always_active_and_updates_name(acc):
    acc.ensure_admin(1, "Pemilik")
    acc.set_status(1, "blocked")          # tak berpengaruh pada admin
    u = acc.get_user(1)
    assert (u.role, u.status, u.display_name) == ("admin", "active", "Pemilik")
    acc.ensure_admin(1, "")
    assert acc.get_user(1).display_name == "Pemilik"   # nama kosong tak menghapus nama lama


def test_ensure_admin_promotes_existing_user(acc):
    make_active(acc, 7)
    acc.ensure_admin(7, "Budi")
    assert acc.get_user(7).role == "admin"


def test_block_unblock_rules(acc):
    make_active(acc)
    assert acc.set_status(5, "blocked") is True and acc.get_user(5).status == "blocked"
    assert acc.set_status(5, "active") is True
    acc.create_pending(6, "Siti", "CODE2345")
    assert acc.set_status(6, "active") is False   # pending harus setuju dulu, tak bisa dilompati admin
    assert acc.get_user(6).status == "pending"
    assert acc.set_status(6, "blocked") is True
    assert acc.set_status(404, "blocked") is False
    with pytest.raises(ValueError):
        acc.set_status(5, "admin")


def test_listing_counts_and_active_ids(acc):
    acc.ensure_admin(1, "Pemilik")
    make_active(acc, 5)
    acc.create_pending(6, "Siti", "CODE2345")
    make_active(acc, 7)
    acc.set_status(7, "blocked")
    assert acc.active_chat_ids() == [1, 5]
    assert acc.counts() == {"active": 2, "pending": 1, "blocked": 1}
    assert {u.chat_id for u in acc.list_users()} == {1, 5, 6, 7}


def test_touch_is_rate_limited(acc, clock, db):
    make_active(acc)
    acc.touch(5)
    first = acc.get_user(5).last_seen
    clock.advance(seconds=10)
    acc.touch(5)
    assert acc.get_user(5).last_seen == first          # <60 detik: tak ditulis lagi
    clock.advance(seconds=120)
    acc.touch(5)
    assert acc.get_user(5).last_seen != first


# ---------- kuota AI ----------
def test_ai_usage_counter(acc):
    assert acc.ai_calls_today(5, "2026-11-18") == 0
    acc.add_ai_call(5, "2026-11-18")
    acc.add_ai_call(5, "2026-11-18")
    acc.add_ai_call(5, "2026-11-19")
    acc.add_ai_call(6, "2026-11-18")
    assert acc.ai_calls_today(5, "2026-11-18") == 2
    assert acc.ai_calls_today(5, "2026-11-19") == 1 and acc.ai_calls_today(6, "2026-11-18") == 1


# ---------- token login & sesi ----------
def test_login_token_single_use_and_expiry(acc, clock):
    token = acc.create_login_token(5, ttl_seconds=600)
    assert acc.consume_login_token(token) == 5
    assert acc.consume_login_token(token) is None
    stale = acc.create_login_token(5, ttl_seconds=600)
    clock.advance(seconds=601)
    assert acc.consume_login_token(stale) is None
    assert acc.consume_login_token("") is None and acc.consume_login_token("tebakan") is None


def test_tokens_and_sessions_are_stored_hashed(acc, db):
    make_active(acc)
    token = acc.create_login_token(5)
    sid = acc.create_session(5, 3600)
    rows = db.query("SELECT token_hash FROM login_tokens") + db.query("SELECT session_hash FROM sessions")
    dumped = " ".join(str(tuple(r)) for r in rows)
    assert token not in dumped and sid not in dumped
    assert hash_token(token) in dumped and hash_token(sid) in dumped


def test_session_validity_expiry_and_logout(acc, clock):
    make_active(acc)
    sid = acc.create_session(5, 3600)
    assert acc.session_chat(sid) == 5
    assert acc.session_chat("") is None and acc.session_chat("palsu") is None
    acc.delete_session(sid)
    assert acc.session_chat(sid) is None
    sid2 = acc.create_session(5, 60)
    clock.advance(seconds=61)
    assert acc.session_chat(sid2) is None


def test_blocked_user_loses_dashboard_sessions(acc):
    make_active(acc)
    sid = acc.create_session(5, 3600)
    acc.set_status(5, "blocked")
    assert acc.session_chat(sid) is None
    acc.set_status(5, "active")
    assert acc.session_chat(sid) is None   # sesi lama sudah dicabut, harus masuk lagi


def test_session_requires_an_active_user(acc):
    sid = acc.create_session(99, 3600)      # tidak ada baris pengguna
    assert acc.session_chat(sid) is None


def test_purge_expired_removes_old_rows(acc, clock, db):
    make_active(acc)
    acc.create_session(5, 10)
    acc.create_login_token(5, 10)
    clock.advance(seconds=11)
    acc.purge_expired()
    assert db.query("SELECT 1 FROM sessions") == [] and db.query("SELECT 1 FROM login_tokens") == []
