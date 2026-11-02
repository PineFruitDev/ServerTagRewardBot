"""Bot lifecycle hooks, exercised without opening a Discord connection.

Bot extends discord.Client, so instantiating one would try to build a real
gateway client. These call the methods unbound against a stand-in self, the
same approach the error-handler tests already use.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from src.core.bot import Bot, CommandTree


def fake_response(status=500, reason="Internal Server Error"):
    return SimpleNamespace(status=status, reason=reason)


async def test_tree_on_error_routes_to_the_bot_handler():
    """The override is the only path Discord uses to report a command failure."""
    handler = AsyncMock()
    tree = SimpleNamespace(client=SimpleNamespace(_on_tree_error=handler))
    interaction, error = object(), RuntimeError("boom")
    await CommandTree.on_error(tree, interaction, error)
    handler.assert_awaited_once_with(interaction, error)


async def test_on_ready_logs_the_connected_user(caplog):
    with caplog.at_level("INFO"):
        await Bot.on_ready(SimpleNamespace(user="tagbot#0001"))
    assert "tagbot#0001" in caplog.text


async def test_sync_commands_returns_how_many_synced():
    tree = SimpleNamespace(sync=AsyncMock(return_value=["a", "b", "c"]))
    assert await Bot.sync_commands(SimpleNamespace(tree=tree)) == 3
    tree.sync.assert_awaited_once()


async def test_sync_commands_handles_an_empty_tree():
    tree = SimpleNamespace(sync=AsyncMock(return_value=[]))
    assert await Bot.sync_commands(SimpleNamespace(tree=tree)) == 0


@pytest.mark.parametrize("done", [True, False])
async def test_tree_error_swallows_a_failed_reply(done):
    """If reporting the error also fails, the handler must not raise again."""
    blow_up = AsyncMock(side_effect=discord.HTTPException(fake_response(), "cannot send"))
    interaction = SimpleNamespace(
        command=SimpleNamespace(name="status"),
        response=SimpleNamespace(is_done=lambda: done, send_message=blow_up),
        followup=SimpleNamespace(send=blow_up),
    )
    # Must return normally rather than propagate the HTTPException.
    await Bot._on_tree_error(None, interaction, RuntimeError("original failure"))
    blow_up.assert_awaited_once()


async def test_tree_error_reports_a_missing_command_name():
    """interaction.command is None for autocomplete and expired interactions."""
    send = AsyncMock()
    interaction = SimpleNamespace(
        command=None,
        response=SimpleNamespace(is_done=lambda: False, send_message=send),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    await Bot._on_tree_error(None, interaction, RuntimeError("boom"))
    assert "error occurred" in send.await_args.args[0]
    assert send.await_args.kwargs["ephemeral"] is True
