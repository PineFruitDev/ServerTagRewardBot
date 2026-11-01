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


def user_permissions(interaction: discord.Interaction) -> discord.Permissions | None:
    """The invoking member's guild-wide permissions, or None if unknowable.

    Member.guild_permissions resolves Discord's two implications already: the
    guild owner and anyone holding Administrator come back with every
    permission set. So a check for one named permission accepts both of those
    too, and no separate owner test is needed anywhere.

    interaction.user is a plain User rather than a Member when no member object
    was sent, and a User carries no permissions at all. Discord resolves the
    invoker's permissions into every guild interaction, so fall back to those.
    """
    if interaction.guild is None:
        return None
    granted = getattr(interaction.user, "guild_permissions", None)
    if granted is not None:
        return granted
    resolved = getattr(interaction, "permissions", None)
    return resolved if isinstance(resolved, discord.Permissions) else None


#: Discord's own labels for the permissions a denial can name. Members know
#: "Manage Server" from the UI, not the manage_guild the API calls it.
PERMISSION_LABELS = {
    "administrator": "Administrator",
    "manage_guild": "Manage Server",
    "manage_roles": "Manage Roles",
}


def permission_label(name: str) -> str:
    return PERMISSION_LABELS.get(name, name.replace("_", " ").title())


def invoked_name(interaction: discord.Interaction, default: str) -> str:
    """The slash name actually used, so a denial names the right command.

    One Command object can register several slash commands (/setup registers
    /disable too), in which case help_info.name is only the first of them.
    """
    command = getattr(interaction, "command", None)
    return getattr(command, "name", None) or default


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

    #: Permissions the INVOKER needs, any one of which is enough.
    #: app_commands.default_permissions only sets the *default* a guild can
    #: reassign at will (Server Settings, Integrations, Command Permissions),
    #: so it cannot be the only gate on a command that changes state. Anything
    #: that writes config or moves roles in bulk re-checks here, where the
    #: guild cannot turn it off.
    required_user_permissions: tuple[str, ...] = ()

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

        # Authorisation before capability. Someone who may not run the command
        # at all should be told that, not handed a report on the bot's own
        # permission problems.
        if self.required_user_permissions and interaction.guild is not None:
            held = user_permissions(interaction)
            if held is None:
                return False, ("I can't read your permissions in this server yet. "
                               "Give me a moment and try again.")
            if not any(getattr(held, perm, False) for perm in self.required_user_permissions):
                needed = " or ".join(permission_label(p) for p in self.required_user_permissions)
                name = invoked_name(interaction, self.name)
                return False, (f"You need **{needed}** to use /{name}. "
                               "Ask someone who manages this server to run it for you.")

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
            log.warning("guard - /%s blocked for %s: %s", self.name, interaction.user, reason)
            await interaction.response.send_message(f"❌ {reason}", ephemeral=True)
        return valid
