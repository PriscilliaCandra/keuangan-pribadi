from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from dotenv import load_dotenv


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    telegram_token: str
    allowed_chat_ids: frozenset[int]
    llm_provider: str | None  # "anthropic" | "ollama" | None
    anthropic_api_key: str | None
    model: str
    ollama_host: str
    ollama_model: str | None
    ollama_vision_model: str | None
    db_path: str
    timezone: str
    reminder_days: int
    imap_host: str | None
    imap_user: str | None
    imap_password: str | None
    imap_folder: str
    email_scan_hours: int
    email_lookback_days: int

    @property
    def llm_enabled(self) -> bool:
        return self.llm_provider is not None

    @property
    def email_enabled(self) -> bool:
        # Membaca email butuh AI untuk mengekstrak tagihan.
        return bool(self.imap_host and self.imap_user and self.imap_password and self.llm_enabled)

    @property
    def llm_label(self) -> str:
        if self.llm_provider == "ollama":
            return f"ollama:{self.ollama_model}" + (
                f" (struk: {self.ollama_vision_model})" if self.ollama_vision_model else "")
        if self.llm_provider == "anthropic":
            return f"anthropic:{self.model}"
        return "off"


def _ids(raw: str) -> frozenset[int]:
    out = set()
    for part in raw.split(","):
        part = part.strip()
        if part:
            try:
                out.add(int(part))
            except ValueError as exc:
                raise ConfigError(f"ALLOWED_CHAT_IDS berisi nilai bukan angka: {part!r}") from exc
    return frozenset(out)


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


def load_config(env: Mapping[str, str] | None = None) -> Config:
    if env is None:
        load_dotenv()
        env = os.environ
    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError("TELEGRAM_BOT_TOKEN belum diisi (lihat .env.example).")
    api_key = env.get("ANTHROPIC_API_KEY", "").strip() or None
    ollama_model = env.get("OLLAMA_MODEL", "").strip() or None
    return Config(
        telegram_token=token,
        allowed_chat_ids=_ids(env.get("ALLOWED_CHAT_IDS", "")),
        llm_provider=_choose_provider(env.get("LLM_PROVIDER", ""), api_key, ollama_model),
        anthropic_api_key=api_key,
        model=env.get("ANTHROPIC_MODEL", "").strip() or "claude-opus-5-5",
        ollama_host=env.get("OLLAMA_HOST", "").strip() or "http://localhost:11434",
        ollama_model=ollama_model,
        ollama_vision_model=env.get("OLLAMA_VISION_MODEL", "").strip() or None,
        db_path=env.get("DB_PATH", "data/keuangan.db"),
        timezone=env.get("TIMEZONE", "Asia/Jakarta"),
        reminder_days=int(env.get("REMINDER_DAYS_AHEAD", "3")),
        imap_host=env.get("IMAP_HOST", "").strip() or None,
        imap_user=env.get("IMAP_USER", "").strip() or None,
        imap_password=env.get("IMAP_PASSWORD", "").strip() or None,
        imap_folder=env.get("IMAP_FOLDER", "INBOX"),
        email_scan_hours=int(env.get("EMAIL_SCAN_EVERY_HOURS", "6")),
        email_lookback_days=int(env.get("EMAIL_LOOKBACK_DAYS", "45")),
    )
