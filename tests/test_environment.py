"""Configuration validation uses only synthetic values, never the host's .env."""

import os

import pytest

from src.services.environment import ENVIRONMENTS, Environment, REQUIRED_VARS


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


@pytest.mark.parametrize("length,valid", [(16, False), (17, True), (20, True), (21, False)])
def test_developer_id_length_boundaries(valid_env, monkeypatch, length, valid):
    monkeypatch.setenv("DEVELOPER_IDS", "1" * length)
    if valid:
        Environment.validate()
        assert Environment.get_config().developer_ids == ["1" * length]
    else:
        with pytest.raises(SystemExit, match="DEVELOPER_IDS"):
            Environment.validate()


@pytest.mark.parametrize("raw,expected", [("TRUE", True), ("false", False)])
def test_startup_sync_parsing(valid_env, monkeypatch, raw, expected):
    monkeypatch.setenv("SYNC_ON_START", raw)
    Environment.validate()
    assert Environment.get_config().sync_on_start is expected


# --------------------------------------------------------------------------- #
# Reporting. A misconfigured deployment usually has more than one problem, and
# the report has to be safe to paste into an issue.
# --------------------------------------------------------------------------- #


def test_every_problem_is_reported_in_one_pass(valid_env, monkeypatch):
    """One restart per problem is a miserable way to configure a bot."""
    monkeypatch.delenv("DISCORD_TOKEN")
    monkeypatch.setenv("TAGBOT_STORE_URL", "http://store.example.invalid")
    monkeypatch.setenv("TAGBOT_STORE_KEY", "short")
    monkeypatch.setenv("MEMBERS_INTENT", "maybe")

    with pytest.raises(SystemExit) as failure:
        Environment.validate()

    report = str(failure.value)
    for name in ("DISCORD_TOKEN", "TAGBOT_STORE_URL", "TAGBOT_STORE_KEY", "MEMBERS_INTENT"):
        assert name in report
    assert "4 configuration problem(s)" in report


def test_a_missing_setting_says_where_to_get_it(valid_env, monkeypatch):
    monkeypatch.delenv("TAGBOT_STORE_URL")
    with pytest.raises(SystemExit, match="deployed tagbot-store Worker"):
        Environment.validate()


@pytest.mark.parametrize("name", REQUIRED_VARS)
def test_a_blank_setting_counts_as_missing(valid_env, monkeypatch, name):
    """An empty .env line is a likelier mistake than a deliberate blank."""
    monkeypatch.setenv(name, "   ")
    with pytest.raises(SystemExit, match=f"{name} is required"):
        Environment.validate()


def test_no_secret_value_appears_in_the_report(valid_env, monkeypatch):
    """The report reaches logs, CI output and issue threads."""
    token, key = "test." + "T" * 60, "test-only-" + "K" * 40
    monkeypatch.setenv("DISCORD_TOKEN", token.replace(".", ""))  # malformed: no dot
    monkeypatch.setenv("TAGBOT_STORE_KEY", key[:8])  # malformed: too short

    with pytest.raises(SystemExit) as failure:
        Environment.validate()

    report = str(failure.value)
    assert "T" * 60 not in report and "K" * 20 not in report
    # Not even a fragment: the logger can only redact whole values, so a
    # masked prefix would survive into the output.
    assert token[:6] not in report and key[:8] not in report


def test_a_malformed_developer_id_is_reported_by_position(valid_env, monkeypatch):
    """A value pasted into the wrong variable could be a credential."""
    monkeypatch.setenv("DEVELOPER_IDS", "123456789012345678,sk-test-not-an-id")
    with pytest.raises(SystemExit) as failure:
        Environment.validate()
    report = str(failure.value)
    assert "DEVELOPER_IDS entry 2" in report
    assert "sk-test-not-an-id" not in report


def test_startup_logs_confirm_credentials_without_echoing_them(valid_env, caplog):
    token = os.environ["DISCORD_TOKEN"]
    with caplog.at_level("INFO"):
        Environment.validate()
    assert "present" in caplog.text
    assert token not in caplog.text and token[:6] not in caplog.text
    assert os.environ["TAGBOT_STORE_KEY"] not in caplog.text
    # The host is useful for spotting a staging URL in production; the key is not.
    assert "store.example.invalid" in caplog.text


# --------------------------------------------------------------------------- #
# Settings that used to be accepted and then silently misbehave
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", ["prod", "dev", "staging", "Production "])
def test_an_unknown_environment_is_reported_but_still_boots(valid_env, monkeypatch, value, caplog):
    """Everything that reads ENVIRONMENT already treats non-development as production.

    Refusing to start over ENVIRONMENT=staging would be an outage with nothing
    behind it, so this warns and carries on.
    """
    monkeypatch.setenv("ENVIRONMENT", value)
    with caplog.at_level("WARNING"):
        Environment.validate()

    recognised = value.strip().lower() in ENVIRONMENTS
    assert ("treating it as production" in caplog.text) is not recognised
    config = Environment.get_config()
    assert config.is_production is recognised
    assert config.is_development is False


@pytest.mark.parametrize("raw,enabled", [("yes", True), ("ON", True), ("1", True),
                                         ("off", False), ("0", False), ("no", False)])
def test_startup_sync_accepts_the_same_booleans_as_the_intent_flag(valid_env, monkeypatch,
                                                                   raw, enabled):
    """SYNC_ON_START=yes used to parse as false without a word of complaint."""
    monkeypatch.setenv("SYNC_ON_START", raw)
    Environment.validate()
    assert Environment.get_config().sync_on_start is enabled


@pytest.mark.parametrize("value", ["maybe", "", "  ", "true-ish"])
def test_a_non_boolean_startup_sync_is_rejected(valid_env, monkeypatch, value):
    monkeypatch.setenv("SYNC_ON_START", value)
    if value.strip():
        with pytest.raises(SystemExit, match="SYNC_ON_START must be a boolean"):
            Environment.validate()
    else:
        # Blank falls back to the documented default rather than failing.
        Environment.validate()
        assert Environment.get_config().sync_on_start is False


def test_config_falls_back_to_documented_defaults_without_validation(valid_env, monkeypatch):
    """get_config is only reached after validate() in main, but must not guess wildly."""
    monkeypatch.setenv("MEMBERS_INTENT", "maybe")
    monkeypatch.setenv("SYNC_ON_START", "maybe")
    config = Environment.get_config()
    assert config.members_intent is True and config.sync_on_start is False


def test_an_implausible_sync_delay_warns_rather_than_blocking_startup(valid_env, monkeypatch,
                                                                      caplog):
    """A long throttle only slows a background sync.

    The one thing worse than a slow sync is making an operator raise their
    request rate against the Discord API just to get the bot to start, so the
    typo that 350000-for-350 represents is reported and then honoured.
    """
    monkeypatch.setenv("SYNC_DELAY_MS", "350000")
    with caplog.at_level("WARNING"):
        Environment.validate()
    assert "350000" in caplog.text and "take hours" in caplog.text
    assert Environment.get_config().sync_delay_ms == 350000


def test_an_ordinary_sync_delay_is_not_flagged(valid_env, monkeypatch, caplog):
    monkeypatch.setenv("SYNC_DELAY_MS", "1000")
    with caplog.at_level("WARNING"):
        Environment.validate()
    assert "take hours" not in caplog.text


@pytest.mark.parametrize("url", ["https://", "https:///path", "not-a-url", "ftp://store.invalid"])
def test_a_store_url_without_a_usable_host_is_rejected(valid_env, monkeypatch, url):
    monkeypatch.setenv("TAGBOT_STORE_URL", url)
    with pytest.raises(SystemExit, match="TAGBOT_STORE_URL"):
        Environment.validate()


def test_surrounding_whitespace_is_trimmed_for_every_consumer(valid_env, monkeypatch):
    """A quoted .env line or a copy-paste newline must not reach Discord."""
    token = "test." + "x" * 60
    monkeypatch.setenv("DISCORD_TOKEN", f"  {token}\n")
    monkeypatch.setenv("TAGBOT_STORE_URL", " https://store.example.invalid ")
    Environment.validate()

    assert Environment.get_config().discord_token == token
    # StoreClient reads the variable itself, so the trim has to persist there.
    assert os.environ["DISCORD_TOKEN"] == token
    assert os.environ["TAGBOT_STORE_URL"] == "https://store.example.invalid"
