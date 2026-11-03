"""Views whose failures reach the user, the way a command's do.

discord.ui.View.on_error logs the exception and returns, so a button that
raises leaves whoever pressed it looking at Discord's "This interaction
failed" with no idea whether anything happened, and leaves the operator with
a library log line that names the view's repr but not the command. Every view
in this bot is attached to a slash command's own reply, so a press that fails
is a command that failed, and it is reported through the same path.

A view's timeout produces that same dead end without any exception being
raised, which is why on_timeout is handled here too. See on_timeout.
"""

from typing import Any

import discord

from src.services.command_errors import report
from src.services.logger import get_logger
from src.services.observability import correlate

log = get_logger("View")


class GuardedView(discord.ui.View):
    """A view whose item callbacks report failures instead of going quiet."""

    #: The slash command this view is sent from. A component interaction
    #: carries no command, so interaction.command is None here and the name has
    #: to come from the view itself for the log line to be worth reading.
    command_name: str = "?"

    #: What replaces the prompt once its timeout has elapsed. A subclass whose
    #: offer can still be taken up somewhere else overrides this to say where,
    #: because the content this is written over is the reply the command sent.
    expiry_notice: str = "⏳ This prompt expired. Run the command again when you're ready."

    def __init__(self, *, origin: discord.Interaction | None = None, **options: Any) -> None:
        super().__init__(**options)
        #: The interaction this view was sent as the reply to, and the only
        #: handle on its message: discord.py keeps no reference of its own for
        #: a view sent through an interaction response, and an ephemeral reply
        #: can be edited through nothing else. Optional so a view can still be
        #: built without one; it then has no message to close (see on_timeout).
        self._origin = origin

    def disable_buttons(self) -> None:
        """Grey out every button, so a closed prompt cannot be pressed again.

        The base Item type has no disabled flag, so buttons are picked out
        rather than assumed. Today's views hold only buttons, so nothing is
        skipped in practice.
        """
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

    async def on_timeout(self) -> None:
        """Close an expired prompt instead of leaving buttons that cannot work.

        discord.py drops a view from its store the moment the timeout elapses
        (View._dispatch_timeout), and an undispatched component interaction is
        never answered at all, so pressing one of these buttons afterwards
        earns Discord's own "This interaction failed". Nothing on the message
        says so: the buttons keep rendering as enabled until it is edited. An
        admin who stepped away from /setup for five minutes therefore came back
        to a live-looking prompt that broke when used and never mentioned the
        command that does the same job.

        The library's default implementation does nothing, so this is the only
        place that can retire the prompt.
        """
        self.disable_buttons()
        if self._origin is None:
            return
        try:
            await self._origin.edit_original_response(content=self.expiry_notice, view=self)
        except (discord.DiscordException, OSError):
            # The token outlives this view's timeout, but the reply can have
            # been dismissed and Discord can refuse. Nobody is waiting on this
            # either: discord.py runs on_timeout in a bare task, where an
            # exception would reach only the library's own logger.
            log.warning("on_timeout - /%s could not retire its expired prompt",
                        self.command_name)

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
