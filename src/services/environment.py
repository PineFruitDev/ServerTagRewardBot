"""Environment validation service.

Ensures all required environment variables are present and valid before
the bot starts, with helpful error messages when they aren't.
"""

import os
import re
from dataclasses import dataclass, field

from src.services.logger import get_logger

log = get_logger("Environment")

REQUIRED_VARS = ("DISCORD_TOKEN", "GUILD_ID", "TAG_ROLE_ID")

OPTIONAL_VARS = {
    "ENVIRONMENT": "production",
    "DEVELOPER_IDS": "",
    "SYNC_ON_START": "true",
    "SYNC_DELAY_MS": "350",
}

_SNOWFLAKE = re.compile(r"^\d{17,20}$")


@dataclass
class Config:
    discord_token: str
    guild_id: str
    tag_role_id: str
    environment: str
    sync_on_start: bool
    sync_delay_ms: int
    developer_ids: list[str] = field(default_factory=list)

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

        missing = []
        invalid = []

        for name in REQUIRED_VARS:
            value = os.getenv(name)
            if not value:
                missing.append(name)
                continue
            if name == "DISCORD_TOKEN" and not cls._is_valid_token(value):
                invalid.append(f"{name} (invalid format)")
            if name in ("GUILD_ID", "TAG_ROLE_ID") and not _SNOWFLAKE.match(value):
                invalid.append(f"{name} (invalid format)")

        for name, default in OPTIONAL_VARS.items():
            if not os.getenv(name):
                os.environ[name] = default
                log.info("validate - Set default for %s: %s", name, default)

        for dev_id in cls._split_ids(os.getenv("DEVELOPER_IDS", "")):
            if not _SNOWFLAKE.match(dev_id):
                invalid.append(f"DEVELOPER_IDS contains invalid ID: {dev_id}")

        if not os.getenv("SYNC_DELAY_MS", "350").isdigit():
            invalid.append("SYNC_DELAY_MS (must be a non-negative number)")

        if missing:
            log.error("validate - Missing required environment variables: %s", ", ".join(missing))
            raise SystemExit(f"Missing required environment variables: {', '.join(missing)}")

        if invalid:
            log.error("validate - Invalid environment variables: %s", ", ".join(invalid))
            raise SystemExit(f"Invalid environment variables: {', '.join(invalid)}")

        log.info("validate - Environment validation passed ✅")
        cls._log_environment_info()

    @classmethod
    def get_config(cls) -> Config:
        if cls._config is None:
            cls._config = Config(
                discord_token=os.environ["DISCORD_TOKEN"],
                guild_id=os.environ["GUILD_ID"],
                tag_role_id=os.environ["TAG_ROLE_ID"],
                environment=os.getenv("ENVIRONMENT", "production"),
                sync_on_start=os.getenv("SYNC_ON_START", "true").lower() == "true",
                sync_delay_ms=int(os.getenv("SYNC_DELAY_MS", "350")),
                developer_ids=cls._split_ids(os.getenv("DEVELOPER_IDS", "")),
            )
        return cls._config

    @staticmethod
    def _split_ids(raw: str) -> list[str]:
        return [part.strip() for part in raw.split(",") if part.strip()]

    @staticmethod
    def _is_valid_token(token: str) -> bool:
        # Bot tokens are long, dotted strings; catch obvious paste mistakes early
        return len(token) > 50 and "." in token

    @classmethod
    def _log_environment_info(cls) -> None:
        token = os.environ["DISCORD_TOKEN"]
        masked = token[:6] + "***" + token[-4:] if len(token) >= 10 else "***"
        log.info("validate - Environment: %s", os.getenv("ENVIRONMENT"))
        log.info("validate - Guild ID: %s", os.getenv("GUILD_ID"))
        log.info("validate - Tag Role ID: %s", os.getenv("TAG_ROLE_ID"))
        log.info("validate - Discord Token: %s", masked)
        dev_ids = cls._split_ids(os.getenv("DEVELOPER_IDS", ""))
        if dev_ids:
            log.info("validate - Developer IDs configured: %d", len(dev_ids))
