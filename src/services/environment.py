"""Environment validation service.

Ensures all required environment variables are present and valid before
the bot starts, with helpful error messages when they aren't. Per-guild
settings (reward roles) and streaks live in tagbot-store (Cloudflare D1),
reached via TAGBOT_STORE_URL with the TAGBOT_STORE_KEY bearer secret.

Two rules shape the reporting. Every setting is checked before anything is
raised, because a fresh deployment usually has more than one thing wrong and
one restart per problem is a miserable way to discover that. And no message
ever echoes a value, not even a masked fragment of one: a startup log or a
pasted CI transcript has to stay safe to share, so a bad setting is described
by name and by what was expected instead.
"""

import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from src.services.logger import get_logger

log = get_logger("Environment")

REQUIRED_VARS = ("DISCORD_TOKEN", "TAGBOT_STORE_URL", "TAGBOT_STORE_KEY")

# Plain http is only allowed for a local `wrangler dev` store
_LOCAL_HTTP = re.compile(r"^http://(localhost|127\.0\.0\.1)(:\d+)?(/|$)")

OPTIONAL_VARS = {
    "ENVIRONMENT": "production",
    "DEVELOPER_IDS": "",
    "SYNC_ON_START": "false",
    "SYNC_DELAY_MS": "350",
    # Request the Server Members privileged intent. If Discord refuses it, the
    # bot falls back to limited mode anyway; set false to skip the attempt.
    "MEMBERS_INTENT": "true",
    # Rate limits. Each is a whole number and each is off at 0; see
    # src/services/cooldowns.py for what the three scopes do.
    "COMMAND_COOLDOWN_SECONDS": "5",
    "GUILD_COMMAND_LIMIT": "20",
    "GUILD_COMMAND_WINDOW_SECONDS": "60",
    "REWARD_COOLDOWN_SECONDS": "30",
}

#: What Config.is_development and Config.is_production compare against. A value
#: outside the set is reported and then read as production, which is what every
#: current consumer already does with it.
ENVIRONMENTS = ("development", "production")

_BOOL_TRUE = ("true", "1", "yes", "on")
_BOOL_FALSE = ("false", "0", "no", "off")

_BOOL_VARS = ("SYNC_ON_START", "MEMBERS_INTENT")

_SNOWFLAKE = re.compile(r"^\d{17,20}$")

#: What to say when a required setting is missing. Kept beside REQUIRED_VARS so
#: a new required var cannot be added without telling operators where to get it.
_EXPECTED = {
    "DISCORD_TOKEN": "the bot token from the Discord Developer Portal",
    "TAGBOT_STORE_URL": "the https:// URL of your deployed tagbot-store Worker",
    "TAGBOT_STORE_KEY": "the bearer secret shared with that Worker",
}

_TOKEN_MIN_LENGTH = 50
_STORE_KEY_MIN_LENGTH = 32

#: Whole-number limits, and what each one counts. The description is what an
#: operator sees when the value will not parse, so it says what the number
#: means rather than restating its name.
_LIMIT_VARS = {
    "COMMAND_COOLDOWN_SECONDS": "whole seconds one member waits between uses of the same "
                                "command in one server, or 0 for no per-member cooldown",
    "GUILD_COMMAND_LIMIT": "a whole number of uses of one command allowed per server per "
                           "window, or 0 for no per-server limit",
    "GUILD_COMMAND_WINDOW_SECONDS": "whole seconds the per-server command window covers, "
                                    "or 0 for no per-server limit",
    "REWARD_COOLDOWN_SECONDS": "whole seconds an applied reward decision suppresses "
                               "repeats of itself, or 0 to apply every repeat",
}

# A full sync waits this long before every member re-fetch, so the value is
# multiplied by the member count. Past a second each it is more likely a typo
# (350000 for 350) than a deliberate throttle, which is worth saying out loud
# without refusing to start over it.
_SYNC_DELAY_WARN_MS = 1000


@dataclass
class Config:
    discord_token: str
    environment: str
    sync_on_start: bool
    sync_delay_ms: int
    members_intent: bool = True
    developer_ids: list[str] = field(default_factory=list)
    #: Rate limits, in the units their variable names give. Defaults match
    #: OPTIONAL_VARS so a Config built directly is the documented one.
    command_cooldown_seconds: int = 5
    guild_command_limit: int = 20
    guild_command_window_seconds: int = 60
    reward_cooldown_seconds: int = 30

    @property
    def is_development(self) -> bool:
        return self.environment == "development"

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


class Environment:
    """Validates configuration once at startup, then serves it everywhere."""

    _config: Config | None = None

    @classmethod
    def validate(cls) -> None:
        """Validate all environment variables. Raises SystemExit on failure."""
        log.info("validate - Validating environment variables...")

        cls._apply_defaults()
        problems = cls._check_required() + cls._check_optional()

        if problems:
            for problem in problems:
                log.error("validate - %s", problem)
            listed = "\n".join(f"  - {problem}" for problem in problems)
            raise SystemExit(
                f"ServerTagRewardBot did not start: {len(problems)} configuration problem(s).\n"
                f"{listed}\n"
                "See the Environment Variables table in README.md. No values are shown above "
                "on purpose; check them in your .env or secret manager."
            )

        log.info("validate - Environment validation passed ✅")
        cls._log_environment_info()

    @classmethod
    def get_config(cls) -> Config:
        if cls._config is None:
            cls._config = Config(
                discord_token=cls._setting("DISCORD_TOKEN"),
                environment=cls._setting("ENVIRONMENT", "production").lower(),
                sync_on_start=cls._as_bool("SYNC_ON_START", default=False),
                sync_delay_ms=cls._as_int("SYNC_DELAY_MS", default=350),
                members_intent=cls._as_bool("MEMBERS_INTENT", default=True),
                developer_ids=cls._split_ids(cls._setting("DEVELOPER_IDS")),
                command_cooldown_seconds=cls._as_int("COMMAND_COOLDOWN_SECONDS", default=5),
                guild_command_limit=cls._as_int("GUILD_COMMAND_LIMIT", default=20),
                guild_command_window_seconds=cls._as_int("GUILD_COMMAND_WINDOW_SECONDS",
                                                         default=60),
                reward_cooldown_seconds=cls._as_int("REWARD_COOLDOWN_SECONDS", default=30),
            )
        return cls._config

    @classmethod
    def _apply_defaults(cls) -> None:
        """Fill in every unset optional var so later checks can read them directly.

        A blank or whitespace-only value counts as unset, the same way the
        required checks read it: `SYNC_ON_START=` in a .env is an unfinished
        line, not a request for different behaviour.
        """
        for name, default in OPTIONAL_VARS.items():
            if not cls._setting(name):
                os.environ[name] = default
                log.info("validate - Set default for %s: %s", name, default)

    @classmethod
    def _check_required(cls) -> list[str]:
        """Report each required setting that is missing or unusable."""
        problems = []
        for name in REQUIRED_VARS:
            value = cls._setting(name)
            if not value:
                problems.append(f"{name} is required but not set ({_EXPECTED[name]})")
                continue
            # Surrounding whitespace survives a copy-paste or a quoted .env line
            # and would otherwise reach Discord inside the token. Normalise it
            # here so every consumer reads the trimmed value.
            os.environ[name] = value
            unusable = cls._unusable(name, value)
            if unusable:
                problems.append(f"{name} is set but unusable: {unusable}")
        return problems

    @classmethod
    def _check_optional(cls) -> list[str]:
        """Report each optional setting whose value cannot be interpreted."""
        problems = []

        # An unrecognised ENVIRONMENT is a warning, not a failure: everything
        # that reads it (currently just the log level) already treats anything
        # other than "development" as production, so refusing to boot over
        # ENVIRONMENT=staging would be an outage with nothing behind it.
        environment = cls._setting("ENVIRONMENT").lower()
        if environment not in ENVIRONMENTS:
            log.warning("validate - ENVIRONMENT is not one of %s; treating it as production",
                        ", ".join(ENVIRONMENTS))

        for name in _BOOL_VARS:
            if cls._setting(name).lower() not in _BOOL_TRUE + _BOOL_FALSE:
                problems.append(f"{name} must be a boolean "
                                f"({'/'.join(_BOOL_TRUE)} or {'/'.join(_BOOL_FALSE)})")

        delay = cls._setting("SYNC_DELAY_MS")
        if not delay.isdigit():
            problems.append("SYNC_DELAY_MS must be a whole number of milliseconds, 0 or more")
        elif int(delay) > _SYNC_DELAY_WARN_MS:
            # Deliberately not an error. A long throttle only makes a
            # background sync slower, and the one thing worse than a slow sync
            # is an operator who has to raise their request rate against the
            # Discord API to get the bot to start.
            log.warning("validate - SYNC_DELAY_MS is %s ms, applied before every member "
                        "re-fetch; a full sync of a large server will take hours", delay)

        for name, expected in _LIMIT_VARS.items():
            if not cls._setting(name).isdigit():
                problems.append(f"{name} must be {expected}")

        # Position rather than value: an ID pasted into the wrong variable could
        # be a credential, and this message goes to the logs.
        for position, dev_id in enumerate(cls._split_ids(cls._setting("DEVELOPER_IDS")), start=1):
            if not _SNOWFLAKE.match(dev_id):
                problems.append(f"DEVELOPER_IDS entry {position} is not a Discord user ID "
                                "(17-20 digits, from Discord's Developer Mode > Copy User ID)")

        return problems

    @classmethod
    def _unusable(cls, name: str, value: str) -> str | None:
        """Why a required value cannot be used, or None when it is fine."""
        if name == "DISCORD_TOKEN":
            # Length and shape only. A near-miss token is still a live
            # credential, so nothing about the value is quoted back.
            if len(value) <= _TOKEN_MIN_LENGTH or "." not in value:
                return (f"a bot token is a dotted string longer than {_TOKEN_MIN_LENGTH} characters "
                        "(Developer Portal > Bot > Reset Token, copied whole)")
        elif name == "TAGBOT_STORE_URL":
            return cls._unusable_store_url(value)
        elif name == "TAGBOT_STORE_KEY":
            if len(value) < _STORE_KEY_MIN_LENGTH:
                # The length of an already-rejected key is the one detail that
                # distinguishes a truncated paste from the wrong variable.
                return (f"expected at least {_STORE_KEY_MIN_LENGTH} characters, got {len(value)}; "
                        "this is the Worker's bearer secret, not the Discord token")
        return None

    @staticmethod
    def _unusable_store_url(value: str) -> str | None:
        if _LOCAL_HTTP.match(value):
            return None
        parts = urlsplit(value)
        if parts.scheme != "https":
            return ("must be an https:// URL; plain http is only accepted for a local "
                    "wrangler dev store on localhost or 127.0.0.1")
        if not parts.hostname:
            return "looks like an https:// URL but has no hostname"
        return None

    @staticmethod
    def _setting(name: str, default: str = "") -> str:
        """The configured value, trimmed. Blank and unset are the same thing."""
        return os.getenv(name, default).strip() or default.strip()

    @classmethod
    def _as_bool(cls, name: str, *, default: bool) -> bool:
        """Read one of the boolean settings. validate() rejects anything else."""
        raw = cls._setting(name).lower()
        if raw in _BOOL_TRUE:
            return True
        if raw in _BOOL_FALSE:
            return False
        return default

    @classmethod
    def _as_int(cls, name: str, *, default: int) -> int:
        """Read one of the whole-number settings. validate() rejects anything else.

        Falls back rather than raising, for the same reason _as_bool does: a
        value this cannot read has already failed validation, and get_config is
        also reachable from tests and tooling that never called it.
        """
        raw = cls._setting(name)
        return int(raw) if raw.isdigit() else default

    @staticmethod
    def _split_ids(raw: str) -> list[str]:
        return [part.strip() for part in raw.split(",") if part.strip()]

    @classmethod
    def _log_environment_info(cls) -> None:
        """Confirm what is configured without reproducing any of it.

        The token and the store key are absent by design, including as masked
        prefixes. A fragment is enough to correlate a leaked credential, and it
        slips past the logger's redaction (which can only match whole values),
        so "present" is all this line is allowed to say.
        """
        log.info("validate - Environment: %s", cls._setting("ENVIRONMENT"))
        log.info("validate - Discord token: present, format accepted (value not logged)")
        log.info("validate - Store key: present, format accepted (value not logged)")
        log.info("validate - Store host: %s", urlsplit(cls._setting("TAGBOT_STORE_URL")).hostname)
        config = cls.get_config()
        log.info("validate - Rate limits: %ss per member per command, %s uses per server per "
                 "%ss, %ss of reward duplicate collapsing (0 means off)",
                 config.command_cooldown_seconds, config.guild_command_limit,
                 config.guild_command_window_seconds, config.reward_cooldown_seconds)
        dev_ids = cls._split_ids(cls._setting("DEVELOPER_IDS"))
        if dev_ids:
            log.info("validate - Developer IDs configured: %d", len(dev_ids))
