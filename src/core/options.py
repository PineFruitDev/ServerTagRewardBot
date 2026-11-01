"""Bounds and shape checks for every value a slash command accepts.

Discord validates an option against the command definition it holds on file,
and discord.py re-checks only part of that locally. The two halves are worth
telling apart, because only one of them is a guarantee:

* An **undeclared choice value** raises before the callback runs:
  ``CommandParameter.transform`` looks the value up in the declared choices and
  raises ``TransformerError`` when it is absent.
* A **Range is declaration-only.** ``RangeTransformer`` extends
  ``IdentityTransformer``, whose ``transform`` returns the value untouched, so
  ``Range`` travels into the registration payload and bounds nothing at
  runtime. Whatever Discord sends reaches the callback verbatim.

The definition Discord validates against is also a cached copy of a past build,
and ``register.py`` is a manual step in this repo ("Run this after adding or
changing commands"), so a build whose options have changed routinely runs
against a stale one. An option's declared bound is therefore the first line and
not the only one: every option is bounded once in the declaration Discord
enforces, and once here, on the value that actually arrived.

The id helpers exist for a narrower reason. ``src/services/tag_sync.py`` has
shape checked every gateway id before letting it reach ``int()`` or the store,
because the store answers a malformed path segment with a flat
``400 invalid guild_id`` that the bot cannot turn into anything useful for the
person who ran the command. Ids taken from slash command options reach that
same store and the same REST calls, and had no such check. ``snowflake`` now
lives here so both paths share one definition of the rule.

Every helper answers in ``Command.validate``'s shape: ``None`` means the value
is usable, a string is the reason to show the user, already worded for them.
Nothing here logs or replies, and no helper reaches the network; the caller
owns all three.
"""

import re
from collections.abc import Iterable

import discord

_SNOWFLAKE = re.compile(r"^\d{1,20}$")

#: Discord ids are unsigned 64-bit, so 20 digits is the widest a real one gets,
#: but not every 20-digit number is one.
MAX_SNOWFLAKE = 2**64 - 1

#: Discord accepts up to 6000 characters in a string option while capping a
#: message body at 2000, and a Range saying otherwise is not enforced at
#: runtime, so anything echoed back is clamped well short of both.
MAX_ECHO = 60

GUILD_SCOPE_REFUSAL = (
    "I can't tell which server this is. Run the command from the server itself."
)


def snowflake(value: object) -> str | None:
    """Return value as a Discord id string, or None when it is not one.

    Gateway payloads are parsed straight from JSON, so every id has to be shape
    checked before it reaches int() or the store.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if value >= 0 else None
    if isinstance(value, str) and _SNOWFLAKE.match(value):
        return value
    return None


def option_id(value: object) -> str | None:
    """A command option's id as a store-safe string, or None when it is not one.

    Stricter than snowflake(), which answers only about shape. Zero and a
    20-digit value above the unsigned 64-bit ceiling both pass the shape check
    while being ids Discord cannot have issued, and both would otherwise be
    written to the store and handed to the REST API as though they were real.

    snowflake() itself is deliberately left alone: it guards gateway payloads,
    where the job is to reject what cannot be parsed rather than to judge
    whether Discord could have minted it.
    """
    shaped = snowflake(value)
    if shaped is None:
        return None
    return shaped if 1 <= int(shaped) <= MAX_SNOWFLAKE else None


def guild_scope(interaction: discord.Interaction) -> str | None:
    """This interaction's guild as a store-safe id, or None when there isn't one.

    Each command that reads or writes per-guild data built this itself with
    str(interaction.guild_id), which turns a missing guild into the literal
    string "None" and posts it to the store as a path segment. guild_only keeps
    that unreachable from today's commands, but it is a class attribute set in
    another file, so the invariant is checked here, next to the use, instead of
    being inherited and assumed.
    """
    return option_id(getattr(interaction, "guild_id", None))


async def require_guild_scope(interaction: discord.Interaction) -> str | None:
    """guild_scope(), replying with the reason when there is no usable guild.

    Returns None exactly when it has already answered the user, so callers read
    like the guard() they already follow: get a value or return.
    """
    scope = guild_scope(interaction)
    if scope is None:
        await interaction.response.send_message(f"❌ {GUILD_SCOPE_REFUSAL}", ephemeral=True)
    return scope


def target_refusal(
    interaction: discord.Interaction,
    target: discord.Role | discord.Member | discord.User,
    label: str,
) -> str | None:
    """Why `target` cannot be acted on here, or None when it can.

    Two things are asked of a role or member an option named:

    * **Its id is one Discord could have issued.** It is about to be written to
      the store and spliced into REST paths, and the store rejects anything else
      with a 400 the bot cannot explain.
    * **It belongs to this server.** discord.py resolves a Role or Member option
      against the invoking guild, so a target from elsewhere is not something a
      user can ask for today. It is asserted rather than assumed because of what
      /setup does with the answer: the role id is persisted as a working
      configuration, and every later grant is an http.add_role against *this*
      guild, so a foreign role would be stored successfully and then refused by
      Discord for every member from then on.

    A target that states no guild is accepted rather than guessed at, and the id
    check above still applies to it. discord.User is the real case: a member
    option falls back to one when the member is not cached, and a User carries
    no guild to compare, so there is nothing to disagree with.
    """
    scope = guild_scope(interaction)
    if scope is None:
        return GUILD_SCOPE_REFUSAL

    if option_id(getattr(target, "id", None)) is None:
        return f"I couldn't read that {label}'s id. Pick it from the list and try again."

    home = getattr(getattr(target, "guild", None), "id", None)
    if home is not None and option_id(home) != scope:
        return f"That {label} isn't from this server. Pick one from this server's list."

    return None


def bounded_choice(value: object, allowed: Iterable[str], default: str) -> str:
    """`value` when it is one of `allowed`, else `default`.

    discord.py already raises on a choice value it never declared, so this is
    the second line rather than the first: it keeps a stale registration from
    reaching a dict lookup or a store query with a value this build does not
    know, and it guarantees the caller a value it can key on.
    """
    return value if isinstance(value, str) and value in tuple(allowed) else default


def safe_echo(raw: str, limit: int = MAX_ECHO) -> str:
    """Make a user-supplied option value safe to quote back at them.

    Clamped because the option's declared length bound is not enforced at
    runtime, and escaped because the value is raw user input. Neither handles
    mentions, so a caller that echoes this pairs it with
    AllowedMentions.none(); escaping alone would still render a live ping.
    """
    collapsed = " ".join(raw.split())
    if len(collapsed) > limit:
        collapsed = collapsed[:limit] + "…"
    return discord.utils.escape_markdown(collapsed) if collapsed else "(blank)"
