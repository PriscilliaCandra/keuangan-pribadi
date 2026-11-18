"""Server dashboard web BACA-SAJA dengan login per pengguna.

Alur masuk: pengguna mengirim /dashboard di Telegram -> bot memberi tautan berisi token sekali
pakai (10 menit) -> halaman login menukarnya lewat POST (bukan saat GET, supaya pengambil
pratinjau tautan tidak menghabiskan token) -> sesi acak di cookie HttpOnly + SameSite=Strict.

Keamanan:
- Hanya mendengarkan alamat lokal; untuk akses publik pasang proxy TLS (Caddy/nginx) di depannya.
- Data selalu milik pemilik sesi (chat_id dari sesi, bukan dari parameter); pengguna yang
  diblokir kehilangan sesinya.
- Token & sesi disimpan sebagai hash; pembatas percobaan login per IP; header Host diperiksa.
- Satu-satunya metode tulis: POST /api/login dan /api/logout (JSON saja). Tak ada jalur mengubah
  data keuangan. CSP ketat; berkas statis dari daftar putih.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .accounts import AccountStore
from .dashboard import build_summary, list_transactions
from .db import Database
from .security import RateLimiter

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
PUBLIC_FILES = {  # boleh dibuka tanpa login (tak berisi data)
    "/login.html": ("login.html", "text/html; charset=utf-8"),
    "/login.js": ("login.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
PRIVATE_FILES = {  # butuh sesi
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
COOKIE = "kp_session"
MAX_BODY = 2048


class _Handler(BaseHTTPRequestHandler):
    server: "DashboardServer"  # type: ignore[assignment]
    protocol_version = "HTTP/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # URL bisa memuat token; jangan dicatat

    # ---------- util ----------
    def _send(self, status: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'self'; style-src 'self'; "
                         "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'")
        if self.server.secure_cookie:
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict[str, Any], extra: dict[str, str] | None = None) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", extra)

    def _client_ip(self) -> str:
        if self.server.trust_proxy:
            forwarded = self.headers.get("X-Forwarded-For", "")
            if forwarded:  # entri paling kanan ditambahkan proxy kita sendiri; kiri bisa dipalsukan klien
                return forwarded.split(",")[-1].strip()
        return self.client_address[0]

    def _cookie_value(self) -> str:
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE:
                return value
        return ""

    def _session_chat(self) -> int | None:
        return self.server.accounts.session_chat(self._cookie_value())

    def _cookie_header(self, value: str, max_age: int) -> str:
        flags = f"{COOKIE}={value}; HttpOnly; SameSite=Strict; Path=/; Max-Age={max_age}"
        return flags + ("; Secure" if self.server.secure_cookie else "")

    def _host_ok(self) -> bool:
        return self.headers.get("Host", "") in self.server.allowed_hosts

    def _static(self, table: dict[str, tuple[str, str]], path: str) -> bool:
        entry = table.get(path)
        if entry is None:
            return False
        name, ctype = entry
        self._send(200, (STATIC_DIR / name).read_bytes(), ctype)
        return True

    # ---------- GET / HEAD ----------
    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            self._json(403, {"error": "host tidak diizinkan"})
            return
        url = urlsplit(self.path)
        path, query = url.path, parse_qs(url.query)
        if self._static(PUBLIC_FILES, path):
            return

        chat = self._session_chat()
        if path == "/":
            if chat is None:  # belum masuk: halaman login (tidak menghabiskan token)
                self._static(PUBLIC_FILES, "/login.html")
            else:
                self._static(PRIVATE_FILES, "/")
            return
        if chat is None:
            self._json(401, {"error": "belum masuk. Kirim /dashboard di Telegram untuk tautan masuk."})
            return
        if self._static(PRIVATE_FILES, path):
            return
        if path == "/api/summary":
            self._respond(lambda: build_summary(self.server.db, chat, _arg(query, "period", "bulan_ini"),
                                                self.server.clock()))
        elif path == "/api/transactions":
            self._respond(lambda: list_transactions(
                self.server.db, chat, _arg(query, "period", "bulan_ini"), self.server.clock(),
                _arg(query, "kind", "semua"), _arg(query, "category", ""), _arg(query, "q", ""),
                _int(query, "limit", 25), _int(query, "offset", 0)))
        else:
            self._json(404, {"error": "tidak ditemukan"})

    def _respond(self, fn: Callable[[], dict[str, Any]]) -> None:
        try:
            self._json(200, fn())
        except ValueError as exc:
            self._json(400, {"error": str(exc)})

    # ---------- POST: hanya login & logout ----------
    def do_POST(self) -> None:  # noqa: N802
        # Badan dibaca (dengan batas) SEBELUM balasan apa pun, supaya koneksi tidak diputus paksa
        # di tengah klien yang masih mengirim.
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        raw = self.rfile.read(length) if 0 <= length <= MAX_BODY else None

        if not self._host_ok():
            self._json(403, {"error": "host tidak diizinkan"})
            return
        path = urlsplit(self.path).path
        if path not in ("/api/login", "/api/logout"):
            self._json(405, {"error": "hanya baca-saja"})
            return
        # Hanya JSON: formulir lintas-situs tidak bisa mengirim tipe ini tanpa preflight CORS.
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            self._json(415, {"error": "gunakan application/json"})
            return
        try:
            if raw is None:
                raise ValueError("badan terlalu besar")
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError("bukan objek")
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"error": "permintaan tidak valid"})
            return
        (self._login if path == "/api/login" else self._logout)(body)

    def _login(self, body: dict[str, Any]) -> None:
        ip, limiter = self._client_ip(), self.server.login_limiter
        if limiter.blocked(ip):
            self._json(429, {"error": "terlalu banyak percobaan, coba lagi nanti"},
                       {"Retry-After": str(limiter.retry_after(ip))})
            return
        token = body.get("token")
        chat = self.server.accounts.consume_login_token(token) if isinstance(token, str) and token else None
        if chat is None:
            limiter.record(ip)
            self._json(401, {"error": "tautan tidak valid atau sudah kedaluwarsa. Kirim /dashboard lagi."})
            return
        limiter.reset(ip)
        ttl = self.server.session_ttl
        sid = self.server.accounts.create_session(chat, ttl)
        self._json(200, {"ok": True}, {"Set-Cookie": self._cookie_header(sid, ttl)})

    def _logout(self, body: dict[str, Any]) -> None:
        sid = self._cookie_value()
        if sid:
            self.server.accounts.delete_session(sid)
        self._json(200, {"ok": True}, {"Set-Cookie": self._cookie_header("", 0)})

    # metode lain ditolak
    def _deny(self) -> None:
        self._json(405, {"error": "hanya baca-saja"})

    do_PUT = do_DELETE = do_PATCH = _deny  # noqa: N815


def _arg(query: dict[str, list[str]], name: str, default: str) -> str:
    return (query.get(name) or [default])[0]


def _int(query: dict[str, list[str]], name: str, default: int) -> int:
    try:
        return int((query.get(name) or [default])[0])
    except ValueError:
        return default


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, db: Database, accounts: AccountStore, clock: Callable[[], date],
                 port: int = 8765, host: str = "127.0.0.1", extra_hosts: frozenset[str] = frozenset(),
                 secure_cookie: bool = False, session_ttl: int = 7 * 86400,
                 trust_proxy: bool = False):
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("dashboard hanya boleh mendengarkan alamat lokal; "
                             "untuk akses publik pasang proxy TLS di depannya")
        super().__init__((host, port), _Handler)
        self.db, self.accounts, self.clock = db, accounts, clock
        self.secure_cookie, self.session_ttl, self.trust_proxy = secure_cookie, session_ttl, trust_proxy
        p = self.server_port
        self.allowed_hosts = {f"localhost:{p}", f"127.0.0.1:{p}", f"[::1]:{p}", *extra_hosts}
        self.login_limiter = RateLimiter(max_events=10, window_s=300)  # 10 gagal / 5 menit / IP
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=lambda: self.serve_forever(poll_interval=0.05),
                                        name="dashboard", daemon=True)
        self._thread.start()
        log.info("Dashboard aktif di port %d (alamat lokal)", self.server_port)

    def stop(self) -> None:
        self.shutdown()
        self.server_close()
