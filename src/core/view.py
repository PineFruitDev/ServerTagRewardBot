"""Views whose failures reach the user, the way a command's do.

discord.ui.View.on_error logs the exception and returns, so a button that
raises leaves whoever pressed it looking at Discord's "This interaction
failed" with no idea whether anything happened, and leaves the operator with
a library log line that names the view's repr but not the command. Every view
in this bot is attached to a slash command's own reply, so a press that fails
is a command that failed, and it is reported through the same path.
"""

from typing import Any

import discord

from src.services.command_errors import report


class GuardedView(discord.ui.View):
    """A view whose item callbacks report failures instead of going quiet."""

    #: The slash command this view is sent from. A component interaction
    #: carries no command, so interaction.command is None here and the name has
    #: to come from the view itself for the log line to be worth reading.
    command_name: str = "?"

    async def on_error(self, interaction: discord.Interaction, error: Exception,
                       item: discord.ui.Item[Any], /) -> None:
        # Item labels are fixed strings from this repo rather than user input,
        # so they are safe to log; an item without one is named by its type.
        label = getattr(item, "label", None) or type(item).__name__
        await report(interaction, error, subject=f"/{self.command_name} ({label})")
