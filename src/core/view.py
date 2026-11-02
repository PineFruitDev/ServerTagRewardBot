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
from src.services.observability import correlate


class GuardedView(discord.ui.View):
    """A view whose item callbacks report failures instead of going quiet."""

    #: The slash command this view is sent from. A component interaction
    #: carries no command, so interaction.command is None here and the name has
    #: to come from the view itself for the log line to be worth reading.
    command_name: str = "?"

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        """Correlate every line this press emits. Never refuses a press.

        discord.py runs this check, the item callback and on_error in one task
        per press (View._scheduled_task), and a context variable set inside a
        task cannot be seen outside it, so binding here covers the whole press
        and nothing after it. It is a check rather than a wrapper only because
        a View offers no after-callback hook: a press gets a correlation id and
        the command name that a component interaction does not carry, but no
        latency line of its own.
        """
        correlate(interaction, self.command_name)
        return True

    async def on_error(self, interaction: discord.Interaction, error: Exception,
                       item: discord.ui.Item[Any], /) -> None:
        # Item labels are fixed strings from this repo rather than user input,
        # so they are safe to log; an item without one is named by its type.
        label = getattr(item, "label", None) or type(item).__name__
        await report(interaction, error, subject=f"/{self.command_name} ({label})")
