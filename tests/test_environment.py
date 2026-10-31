"""Configuration validation uses only synthetic values, never the host's .env."""

import pytest

from src.services.environment import Environment, REQUIRED_VARS


@pytest.fixture
def valid_env(monkeypatch):
    # Explicitly synthetic format-valid placeholders; no real credentials.
    monkeypatch.setenv("DISCORD_TOKEN", "test." + "x" * 60)
    monkeypatch.setenv("TAGBOT_STORE_URL", "https://store.example.invalid")
    monkeypatch.setenv("TAGBOT_STORE_KEY", "test-only-" + "x" * 40)


def test_defaults_and_config_cache(valid_env, monkeypatch):
    Environment.validate()
    config = Environment.get_config()
    assert config.members_intent is True
    assert config.sync_on_start is False
    assert config.sync_delay_ms == 350
    assert config.developer_ids == []
    assert config.is_production and not config.is_development
    monkeypatch.setenv("SYNC_DELAY_MS", "12")
    assert Environment.get_config() is config
    assert config.sync_delay_ms == 350


@pytest.mark.parametrize("name", REQUIRED_VARS)
def test_missing_required_setting_fails(valid_env, monkeypatch, name):
    monkeypatch.delenv(name)
    with pytest.raises(SystemExit, match=name):
        Environment.validate()


@pytest.mark.parametrize("name,value", [
    ("DISCORD_TOKEN", "invalid"), ("TAGBOT_STORE_KEY", "short"),
    ("TAGBOT_STORE_URL", "http://store.example.invalid"),
    ("TAGBOT_STORE_URL", "http://localhost.evil.invalid"),
    ("TAGBOT_STORE_URL", "http://127.0.0.1.evil.invalid"),
    ("DEVELOPER_IDS", "123"), ("MEMBERS_INTENT", "maybe"),
    ("SYNC_DELAY_MS", "-1"), ("SYNC_DELAY_MS", "1.5"),
])
def test_invalid_settings_fail(valid_env, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(SystemExit, match=name):
        Environment.validate()


@pytest.mark.parametrize("url", ["http://localhost:8787", "http://127.0.0.1:8787/", "https://store.example.invalid/"])
def test_local_development_and_https_urls(valid_env, monkeypatch, url):
    monkeypatch.setenv("TAGBOT_STORE_URL", url)
    Environment.validate()


@pytest.mark.parametrize("raw,enabled", [("true", True), (" YES ", True), ("1", True), ("on", True),
                                         ("false", False), (" OFF ", False), ("0", False), ("no", False)])
def test_members_intent_boolean_forms(valid_env, monkeypatch, raw, enabled):
    monkeypatch.setenv("MEMBERS_INTENT", raw)
    Environment.validate()
    assert Environment.get_config().members_intent is enabled


def test_explicit_development_settings(valid_env, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("DEVELOPER_IDS", " 123456789012345678, ,987654321098765432 ")
    monkeypatch.setenv("SYNC_ON_START", "true")
    monkeypatch.setenv("SYNC_DELAY_MS", "0")
    Environment.validate()
    config = Environment.get_config()
    assert config.is_development and not config.is_production
    assert config.developer_ids == ["123456789012345678", "987654321098765432"]
    assert config.sync_on_start and config.sync_delay_ms == 0
