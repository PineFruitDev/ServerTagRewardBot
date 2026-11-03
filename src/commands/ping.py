"""Ping command - confirms the bot is responding, and reports gateway latency.

The latency figure is the one part of this that is not always available.
discord.py measures it between a HEARTBEAT and its HEARTBEAT_ACK, so there is
nothing to report until the first ack has landed, and it says so with a
non-finite float rather than with None (see latency_text). /ping is the command
people run precisely when they are unsure the bot is up, which is exactly the
window where that figure is missing, so the absence is reported as a fact about
the heartbeat rather than allowed to fail the command.
"""

import math

import discord
from discord import app_commands

from src.core.command import Command, CommandHelpInfo

#: What to say instead of a figure. The bot answering at all already proves the
#: gateway is up, so the only thing missing is the measurement.
LATENCY_PENDING = "not measured yet (waiting on the first heartbeat)"


def latency_text(seconds: float) -> str:
    """The gateway latency as display text, or why there is no figure yet.

    discord.py reports two non-finite values here, both of them normal:

    * ``inf`` while no HEARTBEAT_ACK has arrived yet. ``KeepAliveHandler``
      starts its latency at ``inf``, and ``DiscordWebSocket.latency`` returns
      ``inf`` while the keep-alive handler itself does not exist, so every
      connect and every resume has a window of up to one heartbeat interval
      (Discord sends about 41s) where this is the value.
    * ``nan`` when there is no websocket to ask, which ``Client.latency``
      returns while a reconnect is in progress.

    round() raises on both (OverflowError on inf, ValueError on nan), and
    neither is a defect in this bot, so neither may reach the caller: an
    unhandled raise here is reported to the user as "an error occurred" and
    logged as a bug to fix. math.isfinite covers both in one check, the same
    way logger._numeric screens field values before serializing them.
    """
    if not math.isfinite(seconds):
        return LATENCY_PENDING
    # Clamped at zero rather than rendered as a negative: a measurement taken
    # across a clock adjustment is not worth showing a member as "-3ms".
    return f"{max(0, round(seconds * 1000))}ms"


class PingCommand(Command):
    help_info = CommandHelpInfo(
        name="ping",
        description="Check if the bot is responding and get latency information",
        usage="/ping",
        examples=["/ping"],
        category="Utility",
    )

    def register(self, tree: app_commands.CommandTree) -> None:
        @tree.command(name="ping", description="Check if the bot is responding")
        async def ping(interaction: discord.Interaction) -> None:
            async with self.observed(interaction):
                if not await self.guard(interaction):
                    return
                await self.execute(interaction)

    async def execute(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            f"🏓 **Pong!**\n"
            f"💓 **API Latency:** {latency_text(interaction.client.latency)}"
        )
