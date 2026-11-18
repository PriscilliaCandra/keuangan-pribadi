import re
from datetime import timedelta

import httpx
import pytest

from keuangan.accounts import AccountStore
from keuangan.dashboard_server import DashboardServer, STATIC_DIR
from tests.conftest import TODAY

BUDI, SITI, BLOCKED = 5, 6, 7


def d(n):
    return TODAY - timedelta(days=n)


@pytest.fixture
def acc(db):
    a = AccountStore(db)
    for chat, name in ((BUDI, "Budi"), (SITI, "Siti"), (BLOCKED, "Dodi")):
        a.create_pending(chat, name, "CODE2345")
        a.give_consent(chat, "v1")
    a.set_status(BLOCKED, "blocked")
    db.add_expense(BUDI, 25_000, "kopi budi", "Makanan & Minuman", d(0))
    db.add_income(BUDI, 8_000_000, "gaji budi", d(1))
    db.add_expense(SITI, 999_000, "rahasia siti", "Belanja", d(0))
    db.add_income(SITI, 5_000_000, "gaji siti", d(1))
    return a


def start(db, acc, **kw):
    srv = DashboardServer(db, acc, lambda: TODAY, port=0, **kw)
    srv.start()
    return srv


@pytest.fixture
def server(db, acc):
    srv = start(db, acc)
    yield srv
    srv.stop()


def client_for(srv):
    return httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}", follow_redirects=False)


@pytest.fixture
def client(server):
    with client_for(server) as c:
        yield c


def post_login(c, token):
    return c.post("/api/login", json={"token": token})


def login(c, acc, chat=BUDI):
    r = post_login(c, acc.create_login_token(chat))
    assert r.status_code == 200, r.text
    return r


# ---------- tanpa login ----------
def test_unauthenticated_pages(client):
    page = client.get("/")
    assert page.status_code == 200 and "Masuk" in page.text and "Keuangan Pribadi" in page.text
    assert "Rp" not in page.text and "Bulan ini" not in page.text      # tak ada data pada halaman login
    assert client.get("/login.js").status_code == 200 and client.get("/style.css").status_code == 200
    for path in ("/app.js", "/api/summary", "/api/transactions", "/api/summary?period=semua"):
        assert client.get(path).status_code == 401, path


def test_opening_the_link_does_not_consume_the_token(client, acc):
    """Pengambil pratinjau tautan (mis. Telegram) tidak boleh menghabiskan token."""
    token = acc.create_login_token(BUDI)
    for _ in range(3):
        r = client.get(f"/?t={token}")
        assert r.status_code == 200 and "Masuk" in r.text and "Set-Cookie" not in r.headers
    assert post_login(client, token).status_code == 200


# ---------- login ----------
def test_login_sets_strict_httponly_cookie_and_is_single_use(client, acc):
    token = acc.create_login_token(BUDI)
    r = post_login(client, token)
    cookie = r.headers["set-cookie"]
    assert r.json() == {"ok": True}
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/" in cookie
    assert "Secure" not in cookie and token not in cookie
    assert post_login(client, token).status_code == 401           # sekali pakai


def test_wrong_expired_and_malformed_tokens(client, acc, db):
    assert post_login(client, "salah").status_code == 401
    assert post_login(client, "").status_code == 401
    assert client.post("/api/login", json={"token": 123}).status_code == 401
    assert client.post("/api/login", json={}).status_code == 401
    token = acc.create_login_token(BUDI, ttl_seconds=-1)          # sudah kedaluwarsa
    assert post_login(client, token).status_code == 401


@pytest.mark.parametrize("kwargs,status", [
    ({"content": b"token=abc", "headers": {"Content-Type": "application/x-www-form-urlencoded"}}, 415),
    ({"content": b"{}", "headers": {"Content-Type": "text/plain"}}, 415),
    ({"content": b"{bukan json", "headers": {"Content-Type": "application/json"}}, 400),
    ({"content": b"[1,2]", "headers": {"Content-Type": "application/json"}}, 400),
    ({"content": b"{\"token\": \"" + b"a" * 5000 + b"\"}", "headers": {"Content-Type": "application/json"}}, 400),
])
def test_login_rejects_bad_requests(client, kwargs, status):
    assert client.post("/api/login", **kwargs).status_code == status


# ---------- sesi & isolasi ----------
def test_session_unlocks_dashboard_and_api(client, acc):
    login(client, acc)
    page = client.get("/")
    assert page.status_code == 200 and "Selisih" in page.text
    assert client.get("/app.js").status_code == 200
    s = client.get("/api/summary?period=bulan_ini").json()
    assert s["kpi"]["income"] == 8_000_000 and s["kpi"]["expense"] == 25_000
    t = client.get("/api/transactions?period=semua").json()
    assert {x["note"] for x in t["rows"]} == {"kopi budi", "gaji budi"}


def test_each_user_sees_only_their_own_data(server, acc):
    with client_for(server) as budi, client_for(server) as siti:
        login(budi, acc, BUDI)
        login(siti, acc, SITI)
        b = budi.get("/api/summary").json()["kpi"]
        s = siti.get("/api/summary").json()["kpi"]
        assert (b["income"], b["expense"]) == (8_000_000, 25_000)
        assert (s["income"], s["expense"]) == (5_000_000, 999_000)
        notes_b = {x["note"] for x in budi.get("/api/transactions?period=semua&limit=100").json()["rows"]}
        assert "rahasia siti" not in notes_b and "gaji siti" not in notes_b


@pytest.mark.parametrize("param", ["chat=6", "chat=5", "chat_id=6", "user=6"])
def test_chat_is_never_taken_from_the_request(server, acc, param):
    """Pemilik sesi menentukan data; parameter apa pun untuk berpindah pengguna diabaikan."""
    with client_for(server) as budi:
        login(budi, acc, BUDI)
        assert budi.get(f"/api/summary?{param}").json()["kpi"]["income"] == 8_000_000
        assert budi.get(f"/api/transactions?{param}&period=semua").json()["total"] == 2


def test_forged_or_guessed_cookies_are_rejected(client, acc):
    login(client, acc)
    for value in ("", "palsu", "5", "A" * 43):
        r = httpx.get(f"{client.base_url}/api/summary", headers={"Cookie": f"kp_session={value}"})
        assert r.status_code == 401
    # sesi sah dari klien lain tidak bisa ditebak dari id pengguna
    assert httpx.get(f"{client.base_url}/api/summary", headers={"Cookie": "kp_session=" + "x" * 43}).status_code == 401


def test_logout_revokes_session_and_clears_cookie(client, acc):
    login(client, acc)
    r = client.post("/api/logout", json={})
    assert r.status_code == 200 and "Max-Age=0" in r.headers["set-cookie"]
    assert client.get("/api/summary").status_code == 401
    assert client.post("/api/logout", json={}).status_code == 200      # idempoten


def test_logout_only_ends_the_callers_session(server, acc):
    with client_for(server) as budi, client_for(server) as siti:
        login(budi, acc, BUDI)
        login(siti, acc, SITI)
        budi.post("/api/logout", json={})
        assert budi.get("/api/summary").status_code == 401
        assert siti.get("/api/summary").status_code == 200


def test_blocking_a_user_revokes_dashboard_access(client, acc):
    login(client, acc, BUDI)
    assert client.get("/api/summary").status_code == 200
    acc.set_status(BUDI, "blocked")
    assert client.get("/api/summary").status_code == 401
    assert client.get("/app.js").status_code == 401


def test_blocked_user_cannot_log_in_even_with_a_token(client, acc):
    token = acc.create_login_token(BLOCKED)
    assert post_login(client, token).status_code == 200      # token sah ditukar...
    assert client.get("/api/summary").status_code == 401     # ...tapi sesinya tak berlaku bagi akun terblokir


def test_expired_session_is_rejected(db, acc):
    srv = start(db, acc, session_ttl=-1)
    try:
        with client_for(srv) as c:
            login(c, acc)
            assert c.get("/api/summary").status_code == 401
    finally:
        srv.stop()


# ---------- parameter ----------
@pytest.mark.parametrize("path,status", [
    ("/api/summary?period=kapan-kapan", 400),
    ("/api/transactions?kind=aneh", 400),
    ("/tidak-ada", 404),
    ("/../../etc/passwd", 404),
    ("/static/../app.py", 404),
    ("/index.html", 404),
])
def test_error_paths(client, acc, path, status):
    login(client, acc)
    assert client.get(path).status_code == status


def test_bad_numeric_params_fall_back(client, acc):
    login(client, acc)
    r = client.get("/api/transactions?period=semua&limit=abc&offset=zzz").json()
    assert r["limit"] == 25 and r["offset"] == 0


# ---------- metode, header, host ----------
def test_only_login_and_logout_can_post(client, acc):
    login(client, acc)
    assert client.post("/api/summary", json={}).status_code == 405
    assert client.post("/api/anything", json={}).status_code == 405
    assert client.post("/", json={}).status_code == 405
    for method in ("PUT", "DELETE", "PATCH"):
        assert client.request(method, "/api/summary").status_code == 405
    assert client.head("/").status_code == 200


def test_security_headers_everywhere(client, acc):
    for path in ("/", "/login.js", "/api/summary"):
        r = client.get(path)
        csp = r.headers["content-security-policy"]
        assert "default-src 'none'" in csp and "script-src 'self'" in csp and "unsafe-inline" not in csp
        assert r.headers["cache-control"] == "no-store" and r.headers["x-frame-options"] == "DENY"
        assert r.headers["x-content-type-options"] == "nosniff"
        assert "strict-transport-security" not in r.headers            # http lokal: tanpa HSTS


def test_host_header_is_validated(client, acc):
    login(client, acc)
    assert client.get("/api/summary", headers={"Host": "evil.example.com"}).status_code == 403
    assert client.get("/", headers={"Host": "localhost:1"}).status_code == 403
    r = client.post("/api/login", json={"token": acc.create_login_token(BUDI)},
                    headers={"Host": "evil.example.com"})
    assert r.status_code == 403


def test_public_host_and_secure_cookie_mode(db, acc):
    srv = start(db, acc, extra_hosts=frozenset({"uang.contoh.id"}), secure_cookie=True)
    try:
        with client_for(srv) as c:
            r = c.post("/api/login", json={"token": acc.create_login_token(BUDI)},
                       headers={"Host": "uang.contoh.id"})
            assert r.status_code == 200
            assert "Secure" in r.headers["set-cookie"] and "HttpOnly" in r.headers["set-cookie"]
            assert "max-age=31536000" in r.headers["strict-transport-security"]
            assert c.get("/", headers={"Host": "lain.contoh.id"}).status_code == 403
    finally:
        srv.stop()


# ---------- pembatas percobaan ----------
def test_brute_force_login_is_rate_limited(client, acc):
    for _ in range(10):
        assert post_login(client, "tebakan").status_code == 401
    blocked = post_login(client, "tebakan")
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) > 0
    # token yang sah pun ditolak selama diblokir
    assert post_login(client, acc.create_login_token(BUDI)).status_code == 429


def test_successful_login_resets_the_counter(client, acc):
    for _ in range(5):
        post_login(client, "salah")
    assert post_login(client, acc.create_login_token(BUDI)).status_code == 200
    for _ in range(10):
        assert post_login(client, "salah").status_code == 401   # hitungan mulai dari nol lagi


def test_forwarded_header_ignored_unless_proxy_trusted(db, acc):
    srv = start(db, acc)  # trust_proxy=False
    try:
        with client_for(srv) as c:
            for i in range(10):   # penyerang memalsukan IP berbeda di tiap permintaan
                c.post("/api/login", json={"token": "x"}, headers={"X-Forwarded-For": f"9.9.9.{i}"})
            assert c.post("/api/login", json={"token": "x"},
                          headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 429
    finally:
        srv.stop()


def test_trusted_proxy_uses_rightmost_forwarded_address(db, acc):
    srv = start(db, acc, trust_proxy=True)
    try:
        with client_for(srv) as c:
            for i in range(10):   # IP kiri dipalsukan klien; kanan = yang dilihat proxy kita
                c.post("/api/login", json={"token": "x"},
                       headers={"X-Forwarded-For": f"6.6.6.{i}, 203.0.113.7"})
            blocked = c.post("/api/login", json={"token": "x"},
                             headers={"X-Forwarded-For": "7.7.7.7, 203.0.113.7"})
            assert blocked.status_code == 429
            other = c.post("/api/login", json={"token": "x"}, headers={"X-Forwarded-For": "7.7.7.7, 198.51.100.1"})
            assert other.status_code == 401    # klien lain di belakang proxy tidak ikut terblokir
    finally:
        srv.stop()


def test_refuses_non_local_bind(db, acc):
    with pytest.raises(ValueError):
        DashboardServer(db, acc, lambda: TODAY, port=0, host="0.0.0.0")


# ---------- penjaga isi tampilan ----------
def read(name):
    return (STATIC_DIR / name).read_text(encoding="utf-8")


def test_frontend_never_uses_innerhtml_or_external_sources():
    js = read("app.js") + read("login.js")
    pages = read("index.html") + read("login.html")
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert banned not in js, banned
    assert not re.search(r"https?://", pages + read("style.css"))          # tanpa CDN/sumber luar
    assert not re.search(r"<script(?![^>]*\bsrc=)", pages)                  # tanpa skrip inline
    assert "style=" not in pages and "onclick=" not in pages               # tanpa gaya/handler inline


def test_frontend_element_ids_referenced_by_js_exist_in_html():
    for js_name, html_name in (("app.js", "index.html"), ("login.js", "login.html")):
        js, html = read(js_name), read(html_name)
        ids = set(re.findall(r"\$\(\"#([\w-]+)\"\)", js)) | set(re.findall(r"getElementById\(\"([\w-]+)\"\)", js))
        missing = {i for i in ids if f'id="{i}"' not in html}
        assert not missing, (js_name, missing)


def test_login_page_strips_the_token_from_the_address_bar():
    js = read("login.js")
    assert "history.replaceState" in js and "/api/login" in js
    assert "?t=" not in js or "location.search" in js        # token hanya dibaca dari URL, tak ditulis ke mana pun
