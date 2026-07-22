"""CommandManager handles all command operations from a single source of truth."""

from discord import app_commands

from src.core.command import Command
from src.services.logger import get_logger

log = get_logger("CommandManager")


class CommandManager:
    """Loads the command registry and wires every command into the tree."""

    def __init__(self, commands: list[Command]):
        self._commands = commands
        self._command_map = {command.name: command for command in commands}
        log.info("__init__ - Loaded %d commands total", len(commands))

    def register_all(self, tree: app_commands.CommandTree) -> None:
        """Register every command's callbacks against the command tree."""
        for command in self._commands:
            command.register(tree)
            log.info("register_all - Registered: /%s [%s]", command.name, command.help_info.category)

    def all_commands(self) -> list[Command]:
        return list(self._commands)

    def get(self, name: str) -> Command | None:
        return self._command_map.get(name)

    def by_category(self) -> dict[str, list[Command]]:
        categories: dict[str, list[Command]] = {}
        for command in self._commands:
            categories.setdefault(command.help_info.category, []).append(command)
        return categories

    def count(self) -> int:
        return len(self._commands)
