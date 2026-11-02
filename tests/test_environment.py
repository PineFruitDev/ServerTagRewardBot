"""Configuration validation uses only synthetic values, never the host's .env."""

import os
import re

import pytest

from src.services.environment import Environment, OPTIONAL_VARS, REQUIRED_VARS


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
    assert "TAGBOT_STORE_URL" in caplog.text
    assert "store.example.invalid" not in caplog.text


# --------------------------------------------------------------------------- #
# Settings that used to be accepted and then silently misbehave
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", ["prod", "dev", "staging", "private-canary"])
def test_an_unknown_environment_warns_without_blocking_startup(valid_env, monkeypatch, value, caplog):
    monkeypatch.setenv("ENVIRONMENT", value)
    with caplog.at_level("WARNING"):
        Environment.validate()
    assert "using production logging" in caplog.text
    assert re.search(r"\b" + re.escape(value) + r"\b", caplog.text) is None
    assert Environment.get_config().is_development is False


def test_environment_case_and_surrounding_whitespace_are_normalized(valid_env, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "Production ")
    Environment.validate()
    assert Environment.get_config().is_production


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


def test_startup_does_not_trust_a_provisional_config(valid_env, monkeypatch):
    monkeypatch.setenv("MEMBERS_INTENT", "maybe")
    provisional = Environment.get_config()
    with pytest.raises(SystemExit, match="MEMBERS_INTENT"):
        Environment.validate()
    assert Environment._config is None
    monkeypatch.setenv("MEMBERS_INTENT", "false")
    Environment.validate()
    assert Environment.get_config() is not provisional
    assert Environment.get_config().members_intent is False


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
    assert "350000" not in caplog.text and "take hours" in caplog.text
    assert Environment.get_config().sync_delay_ms == 350000


def test_an_ordinary_sync_delay_is_not_flagged(valid_env, monkeypatch, caplog):
    monkeypatch.setenv("SYNC_DELAY_MS", "1000")
    with caplog.at_level("WARNING"):
        Environment.validate()
    assert "take hours" not in caplog.text


# --------------------------------------------------------------------------- #
# Rate limits
# --------------------------------------------------------------------------- #

LIMIT_VARS = ("COMMAND_COOLDOWN_SECONDS", "GUILD_COMMAND_LIMIT",
              "GUILD_COMMAND_WINDOW_SECONDS", "REWARD_COOLDOWN_SECONDS")


def test_rate_limits_default_to_the_documented_values(valid_env):
    Environment.validate()
    config = Environment.get_config()
    assert config.command_cooldown_seconds == 5
    assert (config.guild_command_limit, config.guild_command_window_seconds) == (20, 60)
    assert config.reward_cooldown_seconds == 30


@pytest.mark.parametrize("name", LIMIT_VARS)
@pytest.mark.parametrize("value", ["-1", "1.5", "none", "5s"])
def test_an_unreadable_rate_limit_fails_startup(valid_env, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(SystemExit, match=name):
        Environment.validate()


@pytest.mark.parametrize("name", LIMIT_VARS)
def test_a_rate_limit_of_zero_is_accepted_as_off(valid_env, monkeypatch, name):
    """Turning one limit off must not need a second switch to mean it."""
    monkeypatch.setenv(name, "0")
    Environment.validate()
    assert getattr(Environment.get_config(), name.lower()) == 0


def test_configured_rate_limits_reach_the_config(valid_env, monkeypatch):
    for name, value in zip(LIMIT_VARS, ("30", "5", "120", "0")):
        monkeypatch.setenv(name, value)
    Environment.validate()
    config = Environment.get_config()
    assert config.command_cooldown_seconds == 30
    assert (config.guild_command_limit, config.guild_command_window_seconds) == (5, 120)
    assert config.reward_cooldown_seconds == 0


def test_startup_logs_limit_keys_without_values(valid_env, caplog):
    """Startup logs stay safe even when a credential is pasted into a numeric field."""
    with caplog.at_level("INFO"):
        Environment.validate()
    for name in LIMIT_VARS:
        assert name in caplog.text
    assert "20 uses per server per 60s" not in caplog.text


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


@pytest.mark.parametrize("key", ("SYNC_DELAY_MS", *LIMIT_VARS))
@pytest.mark.parametrize("value", ["²", "١٢", "9" * 5000, "2147483648", "1e5", "1_000"])
def test_numeric_parser_failures_are_named_and_redacted(valid_env, monkeypatch, key, value, caplog):
    monkeypatch.setenv(key, value)
    with pytest.raises(SystemExit, match=key) as failure:
        Environment.validate()
    assert value not in str(failure.value) + caplog.text
    assert Environment._config is None


@pytest.mark.parametrize("value,expected", [("2147483647", 2147483647), ("0" * 5000, 0), ("0007", 7)])
def test_integer_boundaries_are_safe_to_consume(valid_env, monkeypatch, value, expected):
    monkeypatch.setenv("SYNC_DELAY_MS", value)
    Environment.validate()
    assert Environment.get_config().sync_delay_ms == expected


@pytest.mark.parametrize("value", [
    "https://[broken", "https://[not-ip]/", "https://host.invalid:secret-port",
    "https://host.invalid:65536", "http://localhost:99999", "https://host.invalid:0",
    "https://host.invalid:", "https://user:password@host.invalid", "https://host.invalid/?private-query",
    "https://host.invalid/#private-fragment", "https://private host.invalid",
    "https://host.invalid/secret\npath", "https://host.invalid/secret\tpath",
    "https://host.invalid\\private", "https://-bad.invalid", "https://host..invalid",
    "https://host.invalid：secret", "https://[::1]trailing",
])
def test_malformed_url_fails_without_parser_details(valid_env, monkeypatch, value, caplog):
    monkeypatch.setenv("TAGBOT_STORE_URL", value)
    with pytest.raises(SystemExit, match="TAGBOT_STORE_URL") as failure:
        Environment.validate()
    assert value not in str(failure.value) + caplog.text
    assert failure.value.__context__ is None


@pytest.mark.parametrize("value", ["https://[::1]:443", "https://host.invalid:8443/store/", "http://localhost:8787/store"])
def test_supported_store_endpoints_are_accepted(valid_env, monkeypatch, value):
    monkeypatch.setenv("TAGBOT_STORE_URL", value)
    Environment.validate()


@pytest.mark.parametrize("key", ["DISCORD_TOKEN", "TAGBOT_STORE_KEY"])
@pytest.mark.parametrize("bad", ["\n", "\t", " ", "é", "\x7f"])
def test_credentials_reject_embedded_header_invalid_characters(valid_env, monkeypatch, key, bad, caplog):
    value = "synthetic." + "x" * 35 + bad + "y" * 35
    monkeypatch.setenv(key, value)
    with pytest.raises(SystemExit, match=key) as failure:
        Environment.validate()
    assert value not in str(failure.value) + caplog.text


@pytest.mark.parametrize("value", ["18446744073709551616", "00000000000000000", "١" * 18])
def test_unusable_developer_ids_fail(valid_env, monkeypatch, value):
    monkeypatch.setenv("DEVELOPER_IDS", value)
    with pytest.raises(SystemExit, match="DEVELOPER_IDS"):
        Environment.validate()


def test_validation_runs_once_and_returns_the_accepted_snapshot(valid_env, monkeypatch):
    from unittest.mock import Mock

    Environment.validate()
    config = Environment.get_config()
    monkeypatch.setenv("SYNC_DELAY_MS", "private-invalid-value")
    check = Mock(side_effect=AssertionError("must not revalidate"))
    monkeypatch.setattr(Environment, "_check_required", check)
    Environment.validate()
    assert Environment.get_config() is config
    assert config.sync_delay_ms == 350
    check.assert_not_called()


def test_success_logs_name_every_key_without_echoing_values(valid_env, monkeypatch, caplog):
    monkeypatch.setenv("DEVELOPER_IDS", "123456789012345678")
    monkeypatch.setenv("SYNC_DELAY_MS", "7654321")
    for key in LIMIT_VARS:
        monkeypatch.setenv(key, "9876543")
    with caplog.at_level("INFO"):
        Environment.validate()
    for key in (*REQUIRED_VARS, *OPTIONAL_VARS):
        assert key in caplog.text
    for value in ("store.example.invalid", "123456789012345678", "7654321", "9876543"):
        assert value not in caplog.text
    assert os.environ["DISCORD_TOKEN"] not in repr(Environment.get_config())


@pytest.mark.parametrize("entrypoint", ["main", "register"])
def test_malformed_config_aborts_before_any_client_is_created(valid_env, monkeypatch, entrypoint):
    import importlib
    from unittest.mock import Mock

    module = importlib.import_module(entrypoint)
    bot = Mock(side_effect=AssertionError("client must not be constructed"))
    monkeypatch.setattr(module, "Bot", bot)
    if entrypoint == "main":
        monkeypatch.setattr(module, "StoreClient", bot)
    monkeypatch.setenv("TAGBOT_STORE_URL", "https://[private-invalid-url")
    monkeypatch.setenv("SYNC_DELAY_MS", "²")
    with pytest.raises(SystemExit) as failure:
        module.main()
    assert "TAGBOT_STORE_URL" in str(failure.value)
    assert "SYNC_DELAY_MS" in str(failure.value)
    bot.assert_not_called()


def test_valid_provisional_config_is_refreshed_at_startup(valid_env, monkeypatch):
    provisional = Environment.get_config()
    monkeypatch.setenv("SYNC_DELAY_MS", "777")
    Environment.validate()
    assert Environment.get_config() is not provisional
    assert Environment.get_config().sync_delay_ms == 777


@pytest.mark.parametrize("name,value", [("DISCORD_TOKEN", "secret-canary"), ("SYNC_DELAY_MS", "²"),
                                       ("TAGBOT_STORE_URL", "https://[secret-canary")])
def test_validation_failure_logs_do_not_echo_input_without_logger_redaction(valid_env, monkeypatch, name, value):
    from unittest.mock import Mock
    from src.services import environment

    logger = Mock()
    monkeypatch.setattr(environment, "log", logger)
    monkeypatch.setenv(name, value)
    with pytest.raises(SystemExit):
        Environment.validate()
    assert value not in str(logger.mock_calls)
    assert name in str(logger.mock_calls)
