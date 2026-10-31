"""Per-guild configuration store.

The bot is public and serves any number of servers, so each guild's reward
role lives in tagbot-store (Cloudflare D1, encrypted at rest), not on the bot
host. Admins set it with /setup; everything else reads from here.

get_role runs on every member update the bot sees, so results are cached
briefly. This process is the only writer, so local writes update the cache
immediately.
"""

import time

from src.services.logger import get_logger
from src.services.store_client import StoreClient

log = get_logger("GuildConfig")

_CACHE_TTL = 60.0


class GuildConfig:
    """Remote store mapping guild_id -> reward role, with a short read cache."""

    _instance: "GuildConfig | None" = None

    def __init__(self, client: StoreClient | None = None, cache_ttl: float = _CACHE_TTL):
        self._client = client or StoreClient.get_instance()
        self._ttl = cache_ttl
        self._cache: dict[str, tuple[float, str | None]] = {}
        GuildConfig._instance = self
        log.info("__init__ - Using remote store")

    @classmethod
    def get_instance(cls) -> "GuildConfig":
        if cls._instance is None:
            cls._instance = GuildConfig()
        return cls._instance

    def _remember(self, guild_id: str, role_id: str | None) -> None:
        self._cache[guild_id] = (time.monotonic() + self._ttl, role_id)

    async def set_role(self, guild_id: str, role_id: str) -> None:
        guild_id, role_id = str(guild_id), str(role_id)
        await self._client.put(f"/guilds/{guild_id}", {"role_id": role_id})
        self._remember(guild_id, role_id)
        log.info("set_role - Guild %s now rewards role %s", guild_id, role_id)

    async def get_role(self, guild_id: str) -> str | None:
        """Return the reward role for a guild, or None if unconfigured/disabled."""
        guild_id = str(guild_id)
        cached = self._cache.get(guild_id)
        if cached is not None and cached[0] > time.monotonic():
            return cached[1]
        data = await self._client.get(f"/guilds/{guild_id}")
        role_id = data.get("role_id")
        self._remember(guild_id, role_id)
        return role_id

    async def disable(self, guild_id: str) -> bool:
        """Turn the bot off for a guild. Returns True if it was configured."""
        guild_id = str(guild_id)
        data = await self._client.post(f"/guilds/{guild_id}/disable")
        self._remember(guild_id, None)
        log.info("disable - Guild %s disabled", guild_id)
        return bool(data.get("was_configured"))

    async def all_guilds(self) -> list[tuple[str, str]]:
        """All (guild_id, role_id) pairs with the bot enabled."""
        data = await self._client.get("/guilds")
        guilds = [(str(g), str(r)) for g, r in data.get("guilds", [])]
        for guild_id, role_id in guilds:
            self._remember(guild_id, role_id)
        return guilds

    async def count(self) -> int:
        return len(await self.all_guilds())
