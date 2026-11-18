from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit

from dotenv import load_dotenv


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    telegram_token: str
    admin_chat_ids: frozenset[int]  # pemilik/admin bot: selalu aktif, boleh membuat undangan
    llm_provider: str | None  # "anthropic" | "ollama" | None
    anthropic_api_key: str | None
    model: str
    ollama_host: str
    ollama_model: str | None
    ollama_vision_model: str | None
    registration_mode: str       # "invite" (kode undangan) | "open" (siapa pun bisa daftar)
    max_users: int               # batas total pengguna biasa
    signups_per_hour: int        # batas pendaftaran baru per jam (mode terbuka)
    ai_daily_limit: int          # panggilan AI per pengguna per hari (admin bebas)
    ai_max_concurrent: int       # berapa permintaan AI berjalan bersamaan
    ai_wait_seconds: int         # lama menunggu giliran AI sebelum menyerah
    db_path: str
    backup_dir: str
    backup_keep: int
    timezone: str
    reminder_days: int
    imap_host: str | None
    imap_user: str | None
    imap_password: str | None
    imap_folder: str
    email_scan_hours: int
    email_lookback_days: int
    dashboard_port: int                       # 0 = dashboard dimatikan
    dashboard_public_url: str | None          # mis. https://keuangan.contoh.id (di balik proxy TLS)
    dashboard_trust_proxy: bool               # percaya X-Forwarded-For (hanya di balik proxy sendiri)
    dashboard_session_days: int

    @property
    def allowed_chat_ids(self) -> frozenset[int]:  # nama lama
        return self.admin_chat_ids

    @property
    def llm_enabled(self) -> bool:
        return self.llm_provider is not None

    @property
    def email_enabled(self) -> bool:
        # Membaca email butuh AI untuk mengekstrak tagihan. Hanya untuk admin (satu kotak surat).
        return bool(self.imap_host and self.imap_user and self.imap_password and self.llm_enabled)

    @property
    def llm_label(self) -> str:
        if self.llm_provider == "ollama":
            return f"ollama:{self.ollama_model}" + (
                f" (struk: {self.ollama_vision_model})" if self.ollama_vision_model else "")
        if self.llm_provider == "anthropic":
            return f"anthropic:{self.model}"
        return "off"

    @property
    def ai_privacy_note(self) -> str:
        """Kalimat jujur untuk pengguna tentang ke mana data AI-nya pergi."""
        if self.llm_provider == "ollama":
            return "dibaca oleh model AI yang berjalan di server yang sama; tidak dikirim ke pihak ketiga."
        if self.llm_provider == "anthropic":
            return "dikirim ke Anthropic (Claude) untuk diproses; lihat kebijakan privasi Anthropic."
        return "fitur AI sedang tidak aktif."

    @property
    def dashboard_base_url(self) -> str:
        return (self.dashboard_public_url or f"http://localhost:{self.dashboard_port}").rstrip("/")

    @property
    def dashboard_host_names(self) -> frozenset[str]:
        """Host publik yang boleh melayani dashboard (selain localhost)."""
        if not self.dashboard_public_url:
            return frozenset()
        return frozenset({urlsplit(self.dashboard_public_url).netloc})


def _ids(raw: str, name: str) -> frozenset[int]:
    out = set()
    for part in raw.split(","):
        part = part.strip()
        if part:
            try:
                out.add(int(part))
            except ValueError as exc:
                raise ConfigError(f"{name} berisi nilai bukan angka: {part!r}") from exc
    return frozenset(out)


def _int(env: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    raw = env.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError as exc:
        raise ConfigError(f"{name} bukan angka: {raw!r}") from exc
    if not low <= value <= high:
        raise ConfigError(f"{name} harus {low}-{high}")
    return value


def _choose_provider(explicit: str, api_key: str | None, ollama_model: str | None) -> str | None:
    explicit = explicit.strip().lower()
    if explicit in ("", "auto"):
        return "anthropic" if api_key else "ollama" if ollama_model else None
    if explicit == "none":
        return None
    if explicit == "anthropic":
        if not api_key:
            raise ConfigError("LLM_PROVIDER=anthropic tapi ANTHROPIC_API_KEY kosong.")
        return "anthropic"
    if explicit == "ollama":
        if not ollama_model:
            raise ConfigError("LLM_PROVIDER=ollama tapi OLLAMA_MODEL kosong (mis. qwen2.5:7b).")
        return "ollama"
    raise ConfigError(f"LLM_PROVIDER tidak dikenal: {explicit!r} (pilih anthropic / ollama / none).")


def _registration(raw: str) -> str:
    mode = raw.strip().lower() or "invite"
    if mode not in ("invite", "open"):
        raise ConfigError(f"REGISTRATION tidak dikenal: {raw!r} (pilih invite / open)")
    return mode


def _public_url(raw: str) -> str | None:
    raw = raw.strip()
    if not raw:
        return None
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ConfigError("DASHBOARD_PUBLIC_URL harus berbentuk https://domain-kamu")
    return raw.rstrip("/")


def load_config(env: Mapping[str, str] | None = None) -> Config:
    if env is None:
        load_dotenv()
        env = os.environ
    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError("TELEGRAM_BOT_TOKEN belum diisi (lihat .env.example).")
    api_key = env.get("ANTHROPIC_API_KEY", "").strip() or None
    ollama_model = env.get("OLLAMA_MODEL", "").strip() or None
    provider = _choose_provider(env.get("LLM_PROVIDER", ""), api_key, ollama_model)
    # ADMIN_CHAT_IDS menggantikan ALLOWED_CHAT_IDS (nama lama tetap dibaca).
    admins = _ids(env.get("ADMIN_CHAT_IDS", ""), "ADMIN_CHAT_IDS") or \
        _ids(env.get("ALLOWED_CHAT_IDS", ""), "ALLOWED_CHAT_IDS")
    port = _int(env, "DASHBOARD_PORT", 8765, 0, 65535)  # 0 = matikan dashboard
    return Config(
        telegram_token=token,
        admin_chat_ids=admins,
        llm_provider=provider,
        anthropic_api_key=api_key,
        model=env.get("ANTHROPIC_MODEL", "").strip() or "claude-opus-5-5",
        ollama_host=env.get("OLLAMA_HOST", "").strip() or "http://localhost:11434",
        ollama_model=ollama_model,
        ollama_vision_model=env.get("OLLAMA_VISION_MODEL", "").strip() or None,
        registration_mode=_registration(env.get("REGISTRATION", "")),
        max_users=_int(env, "MAX_USERS", 100, 1, 1_000_000),
        signups_per_hour=_int(env, "SIGNUPS_PER_HOUR", 20, 1, 100_000),
        ai_daily_limit=_int(env, "AI_DAILY_LIMIT", 30, 1, 100_000),
        # Ollama memproses satu per satu; API cloud boleh paralel.
        ai_max_concurrent=_int(env, "AI_MAX_CONCURRENT", 1 if provider == "ollama" else 4, 1, 64),
        ai_wait_seconds=_int(env, "AI_QUEUE_WAIT_SECONDS", 90, 1, 3600),
        db_path=env.get("DB_PATH", "data/keuangan.db"),
        backup_dir=env.get("BACKUP_DIR", "data/backups"),
        backup_keep=_int(env, "BACKUP_KEEP", 14, 1, 365),
        timezone=env.get("TIMEZONE", "Asia/Jakarta"),
        reminder_days=int(env.get("REMINDER_DAYS_AHEAD", "3")),
        imap_host=env.get("IMAP_HOST", "").strip() or None,
        imap_user=env.get("IMAP_USER", "").strip() or None,
        imap_password=env.get("IMAP_PASSWORD", "").strip() or None,
        imap_folder=env.get("IMAP_FOLDER", "INBOX"),
        email_scan_hours=int(env.get("EMAIL_SCAN_EVERY_HOURS", "6")),
        email_lookback_days=int(env.get("EMAIL_LOOKBACK_DAYS", "45")),
        dashboard_port=port,
        dashboard_public_url=_public_url(env.get("DASHBOARD_PUBLIC_URL", "")),
        dashboard_trust_proxy=env.get("DASHBOARD_TRUST_PROXY", "").strip().lower() in ("1", "true", "ya"),
        dashboard_session_days=_int(env, "DASHBOARD_SESSION_DAYS", 7, 1, 90),
    )
