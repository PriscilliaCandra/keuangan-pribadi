import time
from datetime import timedelta

import httpx
import pytest

from keuangan.accounts import AccountStore
from keuangan.dashboard_server import DashboardServer
from keuangan.service import Service
from tests.conftest import TODAY

BUDI, SITI, BLOCKED = 5, 6, 7
XHR = {"X-Requested-With": "kp"}


@pytest.fixture
def acc(db):
    a = AccountStore(db)
    for chat, name in ((BUDI, "Budi"), (SITI, "Siti"), (BLOCKED, "Dodi")):
        a.create_pending(chat, name, "CODE2345")
        a.give_consent(chat, "v1")
    return a


@pytest.fixture
def svc(db):
    return Service(db, None, clock=lambda: TODAY)


@pytest.fixture
def server(db, acc, svc):
    srv = DashboardServer(db, acc, lambda: TODAY, port=0, service=svc)
    srv.start()
    yield srv
    srv.stop()


class _Client(httpx.Client):
    def __enter__(self):  # sudah terbuka setelah login; boleh dipakai di blok `with`
        return self


def make_client(server, acc, chat=BUDI):
    c = _Client(base_url=f"http://127.0.0.1:{server.server_port}", follow_redirects=False)
    assert c.post("/api/login", json={"token": acc.create_login_token(chat)}).status_code == 200
    return c


@pytest.fixture
def client(server, acc):
    c = make_client(server, acc)
    yield c
    c.close()


def entry(**over):
    body = {"kind": "keluar", "amount": 35_000, "description": "makan siang", "category": "", "date": ""}
    body.update(over)
    return body


# ---------- catat lewat formulir ----------
def test_add_expense_and_income(client, db):
    r = client.post("/api/entry", json=entry(), headers=XHR)
    assert r.status_code == 200 and "Dicatat" in r.json()["message"]
    r = client.post("/api/entry", json=entry(kind="masuk", amount=8_000_000, description="gaji", category="Gaji"),
                    headers=XHR)
    assert r.status_code == 200 and "Pemasukan dicatat" in r.json()["message"]
    assert db.sum_between(BUDI, TODAY, TODAY + timedelta(days=1)) == 35_000
    rows = client.get("/api/transactions").json()["rows"]
    assert {r["source"] for r in rows} == {"web"}


def test_category_and_date_respected(client, db):
    r = client.post("/api/entry", json=entry(category="transportasi", date=(TODAY - timedelta(days=3)).isoformat()),
                    headers=XHR)
    assert r.status_code == 200
    e = db.expenses_between(BUDI, TODAY - timedelta(days=5), TODAY)[0]
    assert e.category == "Transportasi" and e.spent_on == TODAY - timedelta(days=3)


@pytest.mark.parametrize("over", [
    {"kind": "lain"}, {"amount": 0}, {"amount": -5}, {"amount": 1.5}, {"amount": "35000"}, {"amount": True},
    {"amount": 10**11}, {"category": "Bukan Kategori"}, {"date": "2026-13-01"}, {"date": "kemarin"},
    {"date": "2099-01-01"}, {"date": "1990-01-01"}, {"description": "x" * 201}, {"description": 123},
])
def test_invalid_entries_rejected(client, db, over):
    r = client.post("/api/entry", json=entry(**over), headers=XHR)
    assert r.status_code == 400 and "error" in r.json()
    assert db.recent_entries(BUDI, 5) == []


def test_description_defaults_and_control_chars_stripped(client, db):
    client.post("/api/entry", json=entry(description="", category="Transportasi"), headers=XHR)
    client.post("/api/entry", json=entry(description="a\x00b\nc"), headers=XHR)
    notes = sorted(e.description for _, e in db.recent_entries(BUDI, 5))
    assert notes == ["Transportasi", "a b c"]


# ---------- chat teks bebas ----------
def test_chat_records_like_telegram(client, db):
    r = client.post("/api/chat", json={"text": "kopi 25rb"}, headers=XHR)
    assert r.status_code == 200 and "Dicatat" in r.json()["reply"]
    assert db.sum_between(BUDI, TODAY, TODAY + timedelta(days=1)) == 25_000
    r = client.post("/api/chat", json={"text": "Budi utang 50rb"}, headers=XHR)
    assert "50.000" in r.json()["reply"]


@pytest.mark.parametrize("body", [{}, {"text": ""}, {"text": "   "}, {"text": 5}, {"text": "x" * 501}])
def test_chat_invalid(client, body):
    assert client.post("/api/chat", json=body, headers=XHR).status_code == 400


# ---------- hapus ----------
def test_delete_own_entry(client, db):
    client.post("/api/entry", json=entry(), headers=XHR)
    rid = client.get("/api/transactions").json()["rows"][0]["id"]
    r = client.delete(f"/api/entry/{rid}", headers=XHR)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert db.recent_entries(BUDI, 5) == []
    assert client.delete(f"/api/entry/{rid}", headers=XHR).status_code == 404


@pytest.mark.parametrize("token", ["abc", "1;DROP", "m", "-1", "1" * 20, "%2e%2e"])
def test_delete_bad_id(client, token):
    assert client.delete(f"/api/entry/{token}", headers=XHR).status_code == 400


# ---------- isolasi (anti-IDOR) ----------
def test_cannot_delete_or_see_other_users_entries(server, acc, db):
    siti_id = db.add_expense(SITI, 999_000, "rahasia siti", "Belanja", TODAY)
    siti_inc = db.add_income(SITI, 5_000_000, "gaji siti", TODAY)
    with make_client(server, acc, BUDI) as budi:
        assert budi.delete(f"/api/entry/{siti_id}", headers=XHR).status_code == 404
        assert budi.delete(f"/api/entry/m{siti_inc}", headers=XHR).status_code == 404
        assert "rahasia" not in budi.get("/api/transactions").text
    assert db.get_expense(SITI, siti_id) is not None and db.get_income(SITI, siti_inc) is not None


def test_writes_always_go_to_session_owner(server, acc, db):
    with make_client(server, acc, BUDI) as budi:
        budi.post("/api/entry", json=entry(chat_id=SITI, amount=111), headers=XHR)  # chat_id di badan diabaikan
        budi.post("/api/chat", json={"text": "tes 222rb", "chat_id": SITI}, headers=XHR)
    assert db.recent_entries(SITI, 5) == []
    assert len(db.recent_entries(BUDI, 5)) == 2


# ---------- CSRF & auth ----------
def test_requires_session(server):
    with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}") as anon:
        assert anon.post("/api/entry", json=entry(), headers=XHR).status_code == 401
        assert anon.post("/api/chat", json={"text": "kopi 5rb"}, headers=XHR).status_code == 401
        assert anon.delete("/api/entry/1", headers=XHR).status_code == 401


def test_requires_xhr_header(client, db):
    assert client.post("/api/entry", json=entry()).status_code == 403
    assert client.post("/api/chat", json={"text": "kopi 5rb"}).status_code == 403
    assert client.delete("/api/entry/1").status_code == 403
    assert db.recent_entries(BUDI, 5) == []


def test_foreign_origin_rejected(client, server, db):
    bad = {**XHR, "Origin": "https://evil.example.com"}
    assert client.post("/api/entry", json=entry(), headers=bad).status_code == 403
    assert client.delete("/api/entry/1", headers=bad).status_code == 403
    ok = {**XHR, "Origin": f"http://127.0.0.1:{server.server_port}"}
    assert client.post("/api/entry", json=entry(), headers=ok).status_code == 200


def test_form_encoded_and_wrong_type_rejected(client):
    r = client.post("/api/entry", data={"kind": "keluar", "amount": "5"}, headers=XHR)
    assert r.status_code == 415


def test_blocked_user_loses_write_access(server, acc, db):
    with make_client(server, acc, BLOCKED) as c:
        acc.set_status(BLOCKED, "blocked")
        assert c.post("/api/entry", json=entry(), headers=XHR).status_code == 401
    assert db.recent_entries(BLOCKED, 5) == []


def test_other_methods_still_denied(client):
    for method in ("PUT", "PATCH"):
        assert client.request(method, "/api/entry/1", headers=XHR).status_code == 405
    assert client.delete("/api/summary", headers=XHR).status_code == 405
    assert client.get("/api/entry").status_code == 404


def test_oversized_body_rejected(client):
    r = client.post("/api/chat", json={"text": "a" * 6000}, headers=XHR)
    assert r.status_code == 400


# ---------- pembatasan laju ----------
def test_write_rate_limit_per_user(server, acc):
    with make_client(server, acc, BUDI) as budi, make_client(server, acc, SITI) as siti:
        codes = [budi.post("/api/entry", json=entry(amount=1000), headers=XHR).status_code for _ in range(62)]
        assert codes[:60] == [200] * 60 and codes[60] == 429
        assert siti.post("/api/entry", json=entry(), headers=XHR).status_code == 200  # pengguna lain tak ikut kena


def test_chat_rate_limit(server, acc):
    with make_client(server, acc, BUDI) as c:
        codes = [c.post("/api/chat", json={"text": "kopi 5rb"}, headers=XHR).status_code for _ in range(16)]
    assert codes[:15] == [200] * 15 and codes[15] == 429


# ---------- fitur mati bila tanpa Service ----------
def test_disabled_without_service(db, acc):
    srv = DashboardServer(db, acc, lambda: TODAY, port=0)
    srv.start()
    try:
        with make_client(srv, acc) as c:
            assert c.post("/api/entry", json=entry(), headers=XHR).status_code == 503
            assert c.delete("/api/entry/1", headers=XHR).status_code == 405
    finally:
        srv.stop()
