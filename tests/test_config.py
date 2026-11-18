import pytest

from keuangan.config import ConfigError, load_config


def test_requires_token():
    with pytest.raises(ConfigError):
        load_config({})


def test_defaults_and_flags():
    cfg = load_config({"TELEGRAM_BOT_TOKEN": "t", "ALLOWED_CHAT_IDS": "1, 2"})
    assert cfg.allowed_chat_ids == {1, 2}
    assert not cfg.llm_enabled and not cfg.email_enabled
    assert cfg.model == "claude-opus-5-5" and cfg.reminder_days == 3


def test_email_needs_llm_and_credentials():
    base = {"TELEGRAM_BOT_TOKEN": "t", "IMAP_HOST": "h", "IMAP_USER": "u", "IMAP_PASSWORD": "p"}
    assert not load_config(base).email_enabled
    assert load_config({**base, "ANTHROPIC_API_KEY": "k"}).email_enabled


def test_provider_selection():
    base = {"TELEGRAM_BOT_TOKEN": "t"}
    assert load_config({**base, "OLLAMA_MODEL": "m"}).llm_provider == "ollama"
    assert load_config({**base, "OLLAMA_MODEL": "m", "ANTHROPIC_API_KEY": "k"}).llm_provider == "anthropic"
    assert load_config({**base, "OLLAMA_MODEL": "m", "ANTHROPIC_API_KEY": "k",
                        "LLM_PROVIDER": "ollama"}).llm_provider == "ollama"
    assert load_config({**base, "OLLAMA_MODEL": "m", "LLM_PROVIDER": "none"}).llm_provider is None
    cfg = load_config({**base, "OLLAMA_MODEL": "qwen2.5:7b", "OLLAMA_VISION_MODEL": "qwen2.5vl:7b"})
    assert cfg.llm_label == "ollama:qwen2.5:7b (struk: qwen2.5vl:7b)"


@pytest.mark.parametrize("env", [
    {"LLM_PROVIDER": "ollama"}, {"LLM_PROVIDER": "anthropic"}, {"LLM_PROVIDER": "gpt"},
])
def test_provider_misconfig(env):
    with pytest.raises(ConfigError):
        load_config({"TELEGRAM_BOT_TOKEN": "t", **env})


def test_ollama_enables_email_scan():
    cfg = load_config({"TELEGRAM_BOT_TOKEN": "t", "OLLAMA_MODEL": "m",
                       "IMAP_HOST": "h", "IMAP_USER": "u", "IMAP_PASSWORD": "p"})
    assert cfg.email_enabled


def test_bad_chat_id():
    with pytest.raises(ConfigError):
        load_config({"TELEGRAM_BOT_TOKEN": "t", "ALLOWED_CHAT_IDS": "abc"})


def test_dashboard_defaults_and_overrides():
    base = {"TELEGRAM_BOT_TOKEN": "t"}
    cfg = load_config(base)
    assert cfg.dashboard_port == 8765 and cfg.dashboard_public_url is None
    cfg = load_config({**base, "DASHBOARD_PORT": "0", "DASHBOARD_PUBLIC_URL": "https://uang.contoh.id/"})
    assert cfg.dashboard_port == 0 and cfg.dashboard_public_url == "https://uang.contoh.id"


@pytest.mark.parametrize("value", ["abc", "-1", "70000"])
def test_dashboard_port_validation(value):
    with pytest.raises(ConfigError):
        load_config({"TELEGRAM_BOT_TOKEN": "t", "DASHBOARD_PORT": value})


def test_registration_mode_and_limits():
    base = {"TELEGRAM_BOT_TOKEN": "t"}
    cfg = load_config(base)
    assert (cfg.registration_mode, cfg.max_users, cfg.signups_per_hour) == ("invite", 100, 20)
    cfg = load_config({**base, "REGISTRATION": "OPEN", "MAX_USERS": "50", "SIGNUPS_PER_HOUR": "5"})
    assert (cfg.registration_mode, cfg.max_users, cfg.signups_per_hour) == ("open", 50, 5)


@pytest.mark.parametrize("env", [{"REGISTRATION": "bebas"}, {"MAX_USERS": "0"}, {"SIGNUPS_PER_HOUR": "abc"}])
def test_registration_misconfig(env):
    with pytest.raises(ConfigError):
        load_config({"TELEGRAM_BOT_TOKEN": "t", **env})
