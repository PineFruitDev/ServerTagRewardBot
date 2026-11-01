"""Describing Discord API failures for the logs and for the people using the bot.

discord.py raises HTTPException for every non-2xx REST response. The exception
carries the HTTP status and Discord's own numeric error code, and those are
what tell a misconfiguration (the reward role sitting above the bot, 50013)
apart from an outage (503) or a rate limit the library gave up on (429). Those
cases need opposite responses, so nothing here collapses them into one
"API error".

The status is formatted into the message string rather than passed as a
logging argument on purpose: JsonFormatter replaces exception arguments with
their type name so a response body can never reach the log, which would
otherwise reduce every one of these lines to "HTTPException".
"""

import discord

# Discord error codes worth naming. These are configuration problems a server
# admin can actually fix, which a generic failure reply would hide.
MISSING_PERMISSIONS = 50013
UNKNOWN_ROLE = 10011

# Statuses where trying the same call again later could plausibly succeed.
_TRANSIENT_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


def describe(exc: discord.HTTPException) -> str:
    """A short, secret-free identification of a REST failure, for logs."""
    detail = f"HTTP {exc.status}"
    if exc.code:
        detail += f" (Discord error {exc.code})"
    return detail


def is_transient(exc: discord.HTTPException) -> bool:
    """True when the same request could succeed if repeated later."""
    return exc.status in _TRANSIENT_STATUS


def blocks_whole_guild(exc: discord.HTTPException) -> bool:
    """True when this role write will fail the same way for every other member.

    A 403 means the bot cannot manage the role at all (no Manage Roles, or the
    reward role sits above the bot's highest role), and an unknown-role 404
    means the role is gone. Both are guild-wide and neither fixes itself, so a
    sync should stop rather than repeat the call for thousands of members.

    An unknown-member 404 is the opposite case: it is specific to one member
    who left mid-sync, and the rest of the guild is unaffected.
    """
    if exc.status == 403:
        return True
    return exc.status == 404 and exc.code == UNKNOWN_ROLE


def user_message(exc: discord.HTTPException) -> str:
    """What to tell the person who ran the command, in terms they can act on."""
    if exc.status == 403:
        return (
            "⚠️ Discord refused that. I need the **Manage Roles** permission, and my own "
            "highest role has to sit above the reward role. Check Server Settings > Roles, "
            "then try again."
        )
    if exc.status == 404 and exc.code == UNKNOWN_ROLE:
        return (
            "⚠️ The reward role set for this server no longer exists. "
            "Run /setup role:@Role to point me at a new one."
        )
    if exc.status == 429:
        return (
            "⏳ Discord is rate limiting me right now, so I backed off instead of pushing. "
            "Nothing was lost; please try again in a minute."
        )
    if is_transient(exc):
        return (
            "⚠️ Discord's API is having trouble right now, so I couldn't finish that. "
            "Nothing was lost; please try again shortly."
        )
    return "❌ Discord rejected that request. The details are in the bot's logs."
