"""HTTPS client for tagbot-store, the Cloudflare Worker + D1 that holds all bot data.

Nothing sensitive lives on the bot host: guild settings and streaks are stored
in D1 (encrypted at rest) and reached over HTTPS with a bearer key. Configure
with TAGBOT_STORE_URL and TAGBOT_STORE_KEY.

Transient failures (timeouts, connection errors, 429, 5xx) are retried with
exponential backoff; if the store is still unreachable, StoreUnavailable is
raised so commands can show a friendly message instead of a traceback. When a
response carries Retry-After, that is honoured in place of the guess.
"""

import asyncio
import json
import os
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import aiohttp

from src.services.logger import get_logger

log = get_logger("StoreClient")

_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

# A 429 or 503 may ask for a specific wait. Honour it, but never hold a command
# open for longer than this: past the cap, failing fast and letting the caller
# retry beats sitting on a deferred interaction until Discord expires it.
_MAX_RETRY_AFTER = 30.0


class StoreError(Exception):
    """The store rejected a request (bad input, auth, etc). Not retried."""


class StoreUnavailable(StoreError):
    """The store couldn't be reached after retries."""


class StoreClient:
    """Small async JSON client with timeouts, retries, and backoff."""

    _instance: "StoreClient | None" = None

    def __init__(self, base_url: str | None = None, key: str | None = None, *,
                 timeout: float = 5.0, attempts: int = 4, backoff: float = 0.4):
        base_url = base_url or os.getenv("TAGBOT_STORE_URL", "")
        key = key or os.getenv("TAGBOT_STORE_KEY", "")
        if not base_url or not key:
            raise StoreError("TAGBOT_STORE_URL and TAGBOT_STORE_KEY must be set")
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._attempts = max(1, attempts)
        self._backoff = backoff
        self._session: aiohttp.ClientSession | None = None
        StoreClient._instance = self

    @classmethod
    def get_instance(cls) -> "StoreClient":
        if cls._instance is None:
            cls._instance = StoreClient()
        return cls._instance

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def _get_session(self) -> aiohttp.ClientSession:
        # Created lazily so it binds to the running event loop
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=self._headers, timeout=self._timeout)
        return self._session

    async def request(self, method: str, path: str, body: dict | None = None) -> Any:
        url = f"{self._base}{path}"
        last_error = "unknown error"
        for attempt in range(1, self._attempts + 1):
            # Reset per attempt: a Retry-After only governs the wait after the
            # response that carried it.
            requested_wait: float | None = None
            try:
                async with self._get_session().request(method, url, json=body) as resp:
                    if resp.status < 300:
                        return await resp.json()
                    if resp.status not in _RETRYABLE_STATUS:
                        detail = await self._error_detail(resp)
                        raise StoreError(f"{method} {path} -> HTTP {resp.status}: {detail}")
                    last_error = f"HTTP {resp.status}"
                    requested_wait = self._retry_after(resp)
                    if requested_wait is not None and requested_wait > _MAX_RETRY_AFTER:
                        log.error("request - %s %s asked to wait %.1fs, over the %.0fs cap; giving up",
                                  method, path, requested_wait, _MAX_RETRY_AFTER)
                        raise StoreUnavailable(
                            f"store asked for a {requested_wait:.0f}s wait ({last_error})")
            except StoreError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as exc:
                last_error = type(exc).__name__

            if attempt < self._attempts:
                if requested_wait is not None:
                    delay, source = requested_wait, "Retry-After"
                else:
                    delay = self._backoff * (2 ** (attempt - 1)) * (0.5 + random.random())
                    source = "backoff"
                log.warning("request - %s %s failed (%s), retry %d/%d in %.2fs (%s)",
                            method, path, last_error, attempt, self._attempts - 1, delay, source)
                await asyncio.sleep(delay)

        log.error("request - %s %s gave up after %d attempts (%s)", method, path, self._attempts, last_error)
        raise StoreUnavailable(f"store unreachable ({last_error})")

    @staticmethod
    def _retry_after(resp: aiohttp.ClientResponse) -> float | None:
        """Seconds the store asked us to wait, or None when it did not ask.

        RFC 9110 allows either a number of seconds or an HTTP-date, and
        Cloudflare sends the numeric form on a 429. Both are read; anything
        unparseable is ignored in favour of the computed backoff rather than
        treated as zero, which would retry instantly into a rate limit.
        """
        raw = resp.headers.get("Retry-After")
        if not raw:
            return None
        raw = raw.strip()
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())

    @staticmethod
    async def _error_detail(resp: aiohttp.ClientResponse) -> str:
        try:
            data = await resp.json()
            return str(data.get("error", data) if isinstance(data, dict) else data)
        except (aiohttp.ContentTypeError, ValueError):
            return resp.reason or "error"

    async def get(self, path: str) -> Any:
        return await self.request("GET", path)

    async def post(self, path: str, body: dict | None = None) -> Any:
        return await self.request("POST", path, body or {})

    async def put(self, path: str, body: dict) -> Any:
        return await self.request("PUT", path, body)
