import re
from datetime import date, timedelta

import httpx
import pytest

from keuangan import dashboard
from keuangan.dashboard import DashboardServer, STATIC_DIR, build_summary, list_transactions
from tests.conftest import CHAT, TODAY

TOKEN = "t" * 32


def d(n):
    return TODAY - timedelta(days=n)


@pytest.fixture
def seeded(db):
    db.add_expense(CHAT, 25_000, "kopi kenangan", "Makanan & Minuman", d(0))
    db.add_expense(CHAT, 500_000, "sepatu", "Belanja", d(5))
    db.add_expense(CHAT, 120_000, "makan malam", "Makanan & Minuman", d(2))
    db.add_expense(CHAT, 90_000, "gojek", "Transportasi", d(20))      # Okt
    db.add_expense(CHAT, 40_000, "kopi lagi", "Makanan & Minuman", d(40))  # Okt
    db.add_expense(2, 999_000, "milik orang lain", "Belanja", d(0))
    db.add_income(CHAT, 8_000_000, "gaji", d(10))
    db.add_income(CHAT, 1_500_000, "freelance logo", d(3))
    db.add_income(2, 7_000_000, "gaji orang lain", d(1))
    db.set_budget(CHAT, "Makanan & Minuman", 200_000)
    db.upsert_bill(CHAT, "Netflix", 54_000, 20, "langganan", TODAY)
    return db


# ---------- kalkulasi ----------
def test_summary_kpis_this_month(seeded):
    s = build_summary(seeded, CHAT, "bulan_ini", TODAY)
    k = s["kpi"]
    assert (k["income"], k["expense"], k["net"]) == (9_500_000, 645_000, 8_855_000)
    assert k["savings_rate"] == 93 and k["transactions"] == 5
    assert s["from"] == "2026-11-01" and s["to"] == "2026-11-17"  # tidak melewati hari ini


def test_summary_categories_sorted_with_percentages(seeded):
    s = build_summary(seeded, CHAT, "bulan_ini", TODAY)
    assert [(c["name"], c["total"]) for c in s["expense_by_category"]] == [
        ("Belanja", 500_000), ("Makanan & Minuman", 145_000)]
    assert sum(c["pct"] for c in s["expense_by_category"]) in (99, 100, 101)
    assert [(c["name"], c["total"]) for c in s["income_by_category"]] == [
        ("Gaji", 8_000_000), ("Freelance & Usaha", 1_500_000)]


def test_series_daily_for_short_ranges_and_fills_gaps(seeded):
    s = build_summary(seeded, CHAT, "bulan_ini", TODAY)
    assert s["granularity"] == "day" and len(s["series"]) == 17
    by_key = {p["key"]: p for p in s["series"]}
    assert by_key["2026-11-17"]["expense"] == 25_000
    assert by_key["2026-11-14"]["income"] == 1_500_000
    assert by_key["2026-11-16"] == {"key": "2026-11-16", "label": "16 Nov", "income": 0, "expense": 0}


def test_series_monthly_for_long_ranges(seeded):
    s = build_summary(seeded, CHAT, "3_bulan", TODAY)
    assert s["granularity"] == "month"
    assert [(p["key"], p["expense"]) for p in s["series"]] == [
        ("2026-09", 0), ("2026-10", 130_000), ("2026-11", 645_000)]
    assert s["series"][2]["label"] == "Nov 2026"


def test_all_time_starts_at_first_record(seeded):
    s = build_summary(seeded, CHAT, "semua", TODAY)
    assert s["from"] == d(40).isoformat()
    assert s["kpi"]["expense"] == 775_000


def test_empty_database_is_safe(db):
    s = build_summary(db, CHAT, "bulan_ini", TODAY)
    assert s["kpi"]["net"] == 0 and s["kpi"]["savings_rate"] is None
    assert s["expense_by_category"] == [] and s["top_expenses"] == [] and s["budgets"] == []
    assert build_summary(db, CHAT, "semua", TODAY)["series"]  # tetap ada bulan berjalan


def test_period_comparison(seeded):
    s = build_summary(seeded, CHAT, "bulan_lalu", TODAY)
    assert s["kpi"]["expense"] == 130_000 and s["kpi"]["expense_change"] is None  # tak ada pembanding
    seeded.add_expense(CHAT, 100_000, "x", "Belanja", date(2026, 9, 10))
    s = build_summary(seeded, CHAT, "bulan_lalu", TODAY)
    assert s["kpi"]["expense_change"] == 30  # 130k vs 100k


def test_budgets_bills_and_top_expenses(seeded):
    s = build_summary(seeded, CHAT, "bulan_ini", TODAY)
    assert s["budgets"] == [{"name": "Makanan & Minuman", "spent": 145_000, "limit": 200_000, "pct": 72}]
    assert s["bills"]["items"][0]["name"] == "Netflix" and s["bills"]["items"][0]["days_left"] == 3
    assert s["bills"]["monthly_total"] == 54_000
    assert [t["note"] for t in s["top_expenses"]][:2] == ["sepatu", "makan malam"]


def test_summary_is_per_chat(seeded):
    s = build_summary(seeded, 2, "bulan_ini", TODAY)
    assert s["kpi"]["expense"] == 999_000 and s["kpi"]["income"] == 7_000_000
    assert s["budgets"] == [] and s["bills"]["items"] == []


def test_summary_rejects_unknown_period(db):
    with pytest.raises(ValueError):
        build_summary(db, CHAT, "kapan-kapan", TODAY)


# ---------- transaksi ----------
def test_transactions_filters_sort_and_paginate(seeded):
    r = list_transactions(seeded, CHAT, "semua", TODAY)
    assert r["total"] == 7 and r["rows"][0]["note"] == "kopi kenangan"  # terbaru dulu
    assert {x["kind"] for x in r["rows"]} == {"keluar", "masuk"}
    assert all("_k" not in x for x in r["rows"])
    assert list_transactions(seeded, CHAT, "semua", TODAY, kind="masuk")["total"] == 2
    assert list_transactions(seeded, CHAT, "semua", TODAY, category="makanan & minuman")["total"] == 3
    assert list_transactions(seeded, CHAT, "semua", TODAY, query="KOPI")["total"] == 2
    page2 = list_transactions(seeded, CHAT, "semua", TODAY, limit=3, offset=3)
    assert page2["limit"] == 3 and len(page2["rows"]) == 3 and page2["total"] == 7


def test_transactions_clamp_and_validate(seeded):
    assert list_transactions(seeded, CHAT, "semua", TODAY, limit=10_000)["limit"] == dashboard.MAX_ROWS
    assert list_transactions(seeded, CHAT, "semua", TODAY, limit=-5, offset=-9)["offset"] == 0
    with pytest.raises(ValueError):
        list_transactions(seeded, CHAT, "semua", TODAY, kind="aneh")
    with pytest.raises(ValueError):
        list_transactions(seeded, CHAT, "tak-ada", TODAY)


def test_transactions_per_chat(seeded):
    notes = {x["note"] for x in list_transactions(seeded, CHAT, "semua", TODAY, limit=100)["rows"]}
    assert "milik orang lain" not in notes and "gaji orang lain" not in notes


# ---------- token ----------
def test_token_created_once_and_reused(tmp_path):
    p = tmp_path / "sub" / "token"
    first = dashboard.load_or_create_token(p)
    assert len(first) >= 24 and dashboard.load_or_create_token(p) == first
    p.write_text("pendek", encoding="utf-8")  # terlalu pendek -> dibuat ulang
    assert dashboard.load_or_create_token(p) != "pendek"


# ---------- server ----------
@pytest.fixture
def server(seeded):
    srv = DashboardServer(seeded, frozenset({CHAT}), lambda: TODAY, TOKEN, port=0)
    srv.start()
    yield srv
    srv.stop()


@pytest.fixture
def client(server):
    with httpx.Client(base_url=f"http://127.0.0.1:{server.server_port}", follow_redirects=False) as c:
        yield c


def login(client):
    r = client.get(f"/?t={TOKEN}")
    assert r.status_code == 302 and r.headers["location"] == "/"
    return r


def test_requires_token(client):
    for path in ("/", "/app.js", "/style.css", "/api/summary", "/api/transactions"):
        assert client.get(path).status_code == 401
    assert client.get("/?t=salah").status_code == 401
    assert client.get("/api/summary?t=salah").status_code == 401
    assert client.get("/", headers={"Cookie": "kp_session=salah"}).status_code == 401


def test_login_sets_strict_httponly_cookie_and_cleans_url(client):
    r = login(client)
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and TOKEN in cookie
    assert TOKEN not in r.headers["location"]
    assert client.get("/").status_code == 200  # cookie tersimpan di klien


def test_static_assets_and_security_headers(client):
    login(client)
    page = client.get("/")
    assert page.status_code == 200 and "Keuangan Pribadi" in page.text
    csp = page.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "script-src 'self'" in csp and "unsafe-inline" not in csp
    assert page.headers["cache-control"] == "no-store" and page.headers["x-frame-options"] == "DENY"
    assert page.headers["x-content-type-options"] == "nosniff"
    assert client.get("/app.js").headers["content-type"].startswith("text/javascript")
    assert client.get("/style.css").headers["content-type"].startswith("text/css")


def test_api_summary_and_transactions(client):
    login(client)
    s = client.get("/api/summary?period=bulan_ini").json()
    assert s["kpi"]["income"] == 9_500_000
    t = client.get("/api/transactions?period=semua&kind=masuk&limit=1").json()
    assert t["total"] == 2 and len(t["rows"]) == 1
    assert "periods" in s and "categories" in s


@pytest.mark.parametrize("path,status", [
    ("/api/summary?period=kapan-kapan", 400),
    ("/api/transactions?kind=aneh", 400),
    ("/api/summary?chat=abc", 400),
    ("/api/summary?chat=2", 403),          # chat lain tidak boleh dibaca
    ("/api/transactions?chat=2", 403),
    ("/tidak-ada", 404),
    ("/../../etc/passwd", 404),
    ("/static/../app.py", 404),
    ("/index.html", 404),                   # hanya daftar putih
])
def test_error_paths(client, path, status):
    login(client)
    assert client.get(path).status_code == status


def test_bad_numeric_params_fall_back(client):
    login(client)
    r = client.get("/api/transactions?period=semua&limit=abc&offset=zzz").json()
    assert r["limit"] == 25 and r["offset"] == 0


def test_only_read_methods(client):
    login(client)
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        assert client.request(method, "/api/summary").status_code == 405
    assert client.head("/").status_code == 200


def test_host_header_is_validated_against_dns_rebinding(client):
    login(client)
    assert client.get("/api/summary", headers={"Host": "evil.example.com"}).status_code == 403
    assert client.get("/", headers={"Host": "localhost:1"}).status_code == 403


def test_no_allowed_chat_ids_means_no_data(seeded):
    srv = DashboardServer(seeded, frozenset(), lambda: TODAY, TOKEN, port=0)
    srv.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}") as c:
            c.get(f"/?t={TOKEN}")
            assert c.get("/api/summary").status_code == 403
    finally:
        srv.stop()


def test_refuses_non_local_bind(db):
    with pytest.raises(ValueError):
        DashboardServer(db, frozenset({CHAT}), lambda: TODAY, TOKEN, port=0, host="0.0.0.0")


def test_default_chat_is_the_lowest_allowed_id(seeded):
    srv = DashboardServer(seeded, frozenset({2, CHAT}), lambda: TODAY, TOKEN, port=0)
    srv.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{srv.server_port}") as c:
            c.get(f"/?t={TOKEN}")
            assert c.get("/api/summary").json()["kpi"]["income"] == 9_500_000
            assert c.get("/api/summary?chat=2").json()["kpi"]["income"] == 7_000_000
    finally:
        srv.stop()


# ---------- penjaga isi tampilan ----------
def test_frontend_never_uses_innerhtml_or_external_sources():
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert banned not in js, banned
    assert not re.search(r"https?://", html + css)             # tanpa CDN/sumber luar
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)       # tanpa skrip inline
    assert "style=" not in html and "onclick=" not in html     # tanpa gaya/handler inline


def test_frontend_element_ids_referenced_by_js_exist_in_html():
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    ids = set(re.findall(r"\$\(\"#([\w-]+)\"\)", js))
    missing = {i for i in ids if f'id="{i}"' not in html}
    assert not missing, missing
