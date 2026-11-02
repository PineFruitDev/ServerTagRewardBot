"""Abstract command base.

Each command extends Command, describes itself with CommandHelpInfo, and
registers its own slash command against the tree. Validation (guild-only,
permissions, developer-only) is shared here so commands stay tiny, and so is
observation: every registered callback opens with `async with
self.observed(interaction)` so its log lines are correlated and its outcome
and latency are recorded without each command repeating that.
"""

from abc import ABC, abstractmethod
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field

import discord
from discord import app_commands

from src.services.environment import Environment
from src.services.logger import get_logger
from src.services.observability import DENIED, CommandRun, mark, observed

log = get_logger("Command")


def bot_permissions(interaction: discord.Interaction) -> discord.Permissions | None:
    """The bot's permissions in the interaction's guild, or None if unknowable.

    Guild.me is the authoritative guild-wide set, but it is annotated as Member
    while really being get_member(self), which returns None when the self member
    was never cached (this bot runs without the members intent in limited mode).
    Discord sends app_permissions with every guild interaction, so fall back to
    that instead of raising AttributeError on None.
    """
    guild = interaction.guild
    if guild is None:
        return None
    me = guild.me
    if me is not None:
        return me.guild_permissions
    granted = getattr(interaction, "app_permissions", None)
    return granted if isinstance(granted, discord.Permissions) else None


@dataclass
class CommandHelpInfo:
    """Help information for the auto-generated help system."""

    name: str
    description: str
    usage: str
    examples: list[str] = field(default_factory=list)
    category: str = "General"


class Command(ABC):
    """Abstract base class for all slash commands."""

    help_info: CommandHelpInfo

    #: Permissions the BOT needs for this command (guild_permissions attribute names)
    required_permissions: tuple[str, ...] = ()

    #: Whether this command can only be used in guilds (not DMs)
    guild_only: bool = False

    #: Whether this command can only be used by developers
    developer_only: bool = False

    @property
    def name(self) -> str:
        return self.help_info.name

    @property
    def description(self) -> str:
        return self.help_info.description

    @abstractmethod
    def register(self, tree: app_commands.CommandTree) -> None:
        """Attach this command's callback(s) to the command tree."""

    def observed(self, interaction: discord.Interaction,
                 command: str | None = None) -> AbstractAsyncContextManager[CommandRun]:
        """Correlate and time one invocation. Wrap every registered callback.

        Pass `command` for a callback whose slash command is not named after
        this Command, which is how /disable is told apart from the /setup it
        is registered alongside.
        """
        return observed(interaction, command or self.name)

    async def validate(self, interaction: discord.Interaction) -> tuple[bool, str | None]:
        """Check whether the command can run in the current context."""
        if self.guild_only and interaction.guild is None:
            return False, "This command can only be used in servers."

        if self.required_permissions and interaction.guild is not None:
            granted = bot_permissions(interaction)
            if granted is None:
                return False, ("I can't read my own permissions in this server yet. "
                               "Give me a moment and try again.")
            missing = [
                perm for perm in self.required_permissions
                if not getattr(granted, perm, False)
            ]
            if missing:
                return False, f"Bot missing required permissions: {', '.join(missing)}"

        if self.developer_only:
            config = Environment.get_config()
            if str(interaction.user.id) not in config.developer_ids:
                return False, "This command is for developers only."

        return True, None

    async def guard(self, interaction: discord.Interaction) -> bool:
        """Validate and report failures to the user. Returns True when clear to run."""
        valid, reason = await self.validate(interaction)
        if not valid:
            # The reason is one of this module's own fixed strings, and who was
            # refused is already on the line as the user field, so nothing
            # user-controlled (a display name least of all) goes in here.
            mark(DENIED)
            log.warning("guard - /%s refused: %s", self.name, reason)
            await interaction.response.send_message(f"❌ {reason}", ephemeral=True)
        return valid
