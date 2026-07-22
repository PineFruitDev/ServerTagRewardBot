"""Central command registry - SINGLE SOURCE OF TRUTH.

Add new commands here and they'll automatically be registered,
validated, and listed in the help system.
"""

from src.core.command import Command
from src.commands.ping import PingCommand
from src.commands.repped import ReppedCommand
from src.commands.leaderboard import LeaderboardCommand
from src.commands.setup import SetupCommand
from src.commands.status import StatusCommand
from src.commands.sync import SyncCommand
from src.commands.help_command import HelpCommand


def build_registry() -> list[Command]:
    commands: list[Command] = [
        SetupCommand(),
        StatusCommand(),
        SyncCommand(),
        ReppedCommand(),
        LeaderboardCommand(),
        PingCommand(),
        # Add new commands here - they'll automatically be registered and available
    ]
    commands.append(HelpCommand(commands))
    return commands


ALL_COMMANDS: list[Command] = build_registry()
