"""Abstract command base.

Each command extends Command, describes itself with CommandHelpInfo, and
registers its own slash command against the tree. Validation (guild-only,
permissions, developer-only) is shared here so commands stay tiny.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import discord
from discord import app_commands

from src.services.environment import Environment
from src.services.logger import get_logger

log = get_logger("Command")


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

    async def validate(self, interaction: discord.Interaction) -> tuple[bool, str | None]:
        """Check whether the command can run in the current context."""
        if self.guild_only and interaction.guild is None:
            return False, "This command can only be used in servers."

        if self.required_permissions and interaction.guild is not None:
            me = interaction.guild.me
            missing = [
                perm for perm in self.required_permissions
                if not getattr(me.guild_permissions, perm, False)
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
            log.warning("guard - /%s blocked for %s: %s", self.name, interaction.user, reason)
            await interaction.response.send_message(f"❌ {reason}", ephemeral=True)
        return valid
