"""Command boundary checks; all interactions and Discord HTTP calls are fakes."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from src.commands.setup import SetupCommand, SyncPromptView
from src.commands.status import StatusCommand
from src.commands.sync import SyncCommand
from src.core.bot import Bot
from src.core.command import bot_permissions
from src.services.environment import Environment
from src.services.guild_config import GuildConfig
from src.services.store_client import StoreError, StoreUnavailable
from src.services.tag_sync import GuildStats, SYNC_PAUSED_MESSAGE, TagSyncService


@pytest.fixture
def interaction():
    return SimpleNamespace(
        guild_id=1, guild=SimpleNamespace(id=1, me=SimpleNamespace(
            top_role=10, guild_permissions=SimpleNamespace(manage_roles=True),
        )), user=SimpleNamespace(id=42), command=SimpleNamespace(name="sync"),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock(),
                                 edit_message=AsyncMock(), is_done=lambda: False),
        followup=SimpleNamespace(send=AsyncMock()),
    )


@pytest.mark.parametrize("failure", ["dm", "permission", "developer"])
async def test_guard_rejects_unavailable_context(interaction, monkeypatch, failure):
    command = SyncCommand()
    if failure == "dm":
        interaction.guild = None
    elif failure == "permission":
        interaction.guild.me.guild_permissions.manage_roles = False
    else:
        command.developer_only = True
        monkeypatch.setattr(Environment, "_config", SimpleNamespace(developer_ids=["99"]))
    assert await command.guard(interaction) is False
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True


# Guild.me is annotated as Member but is really get_member(self), which is None
# whenever the self member was never cached - the normal case in limited mode.

async def test_permission_check_falls_back_to_app_permissions(interaction):
    interaction.guild.me = None
    interaction.app_permissions = discord.Permissions(manage_roles=True)
    assert await SyncCommand().guard(interaction) is True
    interaction.response.send_message.assert_not_awaited()


async def test_app_permissions_fallback_still_catches_a_missing_permission(interaction):
    interaction.guild.me = None
    interaction.app_permissions = discord.Permissions.none()
    assert await SyncCommand().guard(interaction) is False
    assert "manage_roles" in interaction.response.send_message.await_args.args[0]


async def test_unknowable_permissions_are_reported_not_raised(interaction):
    interaction.guild.me = None  # and no app_permissions on the interaction
    assert await SyncCommand().guard(interaction) is False
    assert "read my own permissions" in interaction.response.send_message.await_args.args[0]
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True


def test_a_guildless_interaction_grants_no_permissions():
    assert bot_permissions(SimpleNamespace(guild=None)) is None


async def test_guard_allows_configured_developer(interaction, monkeypatch):
    command = SyncCommand()
    command.developer_only = True
    monkeypatch.setattr(Environment, "_config", SimpleNamespace(developer_ids=["42"]))
    assert await command.guard(interaction) is True
    interaction.response.send_message.assert_not_awaited()


class Role:
    def __init__(self, *, default=False, managed=False, position=1):
        self.id, self.mention = 10, "<@&10>"
        self.default, self.managed, self.position = default, managed, position

    def is_default(self):
        return self.default

    def __ge__(self, position):
        return self.position >= position


@pytest.mark.parametrize("role", [Role(default=True), Role(managed=True), Role(position=10), Role(position=11)])
async def test_setup_rejects_unassignable_roles(interaction, monkeypatch, role):
    store = SimpleNamespace(set_role=AsyncMock())
    monkeypatch.setattr(GuildConfig, "_instance", store)
    await SetupCommand()._configure(interaction, role)
    store.set_role.assert_not_awaited()
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True


async def test_setup_explains_itself_when_its_own_roles_are_unknown(interaction, monkeypatch):
    """Comparing role ranks needs guild.me, which can legitimately be None."""
    store = SimpleNamespace(set_role=AsyncMock())
    monkeypatch.setattr(GuildConfig, "_instance", store)
    interaction.guild.me = None
    await SetupCommand()._configure(interaction, Role())
    store.set_role.assert_not_awaited()
    assert "my own roles" in interaction.response.send_message.await_args.args[0]
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True


async def test_setup_saves_role_and_offers_sync(interaction, monkeypatch):
    store = SimpleNamespace(set_role=AsyncMock())
    monkeypatch.setattr(GuildConfig, "_instance", store)
    await SetupCommand()._configure(interaction, Role())
    store.set_role.assert_awaited_once_with("1", "10")
    view = interaction.response.send_message.await_args.kwargs["view"]
    assert isinstance(view, SyncPromptView) and view.guild_id == "1"
    view.stop()


@pytest.mark.parametrize("command", [SyncCommand, StatusCommand])
async def test_commands_report_limited_mode(interaction, monkeypatch, command):
    store = SimpleNamespace(get_role=AsyncMock(return_value="10"))
    monkeypatch.setattr(GuildConfig, "_instance", store)
    await command().execute(interaction)
    message = interaction.response.send_message.await_args.args[0]
    assert SYNC_PAUSED_MESSAGE in message
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True
    interaction.response.defer.assert_not_awaited()


@pytest.mark.parametrize("configured,busy", [(False, False), (True, True), (True, False)])
async def test_manual_sync_only_runs_when_ready(interaction, monkeypatch, configured, busy):
    monkeypatch.setattr(GuildConfig, "_instance", SimpleNamespace(
        get_role=AsyncMock(return_value="10" if configured else None),
    ))
    service = SimpleNamespace(is_syncing=lambda gid: busy, full_sync=AsyncMock(return_value=GuildStats()))
    monkeypatch.setattr(TagSyncService, "_instance", service)
    await SyncCommand().execute(interaction)
    if configured and not busy:
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        service.full_sync.assert_awaited_once_with("1")
        interaction.followup.send.assert_awaited_once()
    else:
        service.full_sync.assert_not_awaited()
        interaction.response.defer.assert_not_awaited()


@pytest.mark.parametrize("done", [True, False])
@pytest.mark.parametrize("error,fragment", [
    (StoreUnavailable("offline"), "can't reach my storage"),
    (StoreError("bad request"), "storage rejected"),
    (RuntimeError("failure"), "error occurred"),
])
async def test_command_errors_choose_response_or_followup(interaction, error, fragment, done):
    interaction.response.is_done = lambda: done
    # Use the real error handler without opening a Discord client connection.
    await Bot._on_tree_error(None, interaction, SimpleNamespace(original=error))
    target = interaction.followup.send if done else interaction.response.send_message
    other = interaction.response.send_message if done else interaction.followup.send
    assert fragment in target.await_args.args[0]
    assert target.await_args.kwargs["ephemeral"] is True
    other.assert_not_awaited()
