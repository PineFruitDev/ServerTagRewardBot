"""Offline coverage of admin commands, slash wiring, and the command registry.

The slash callbacks built in register() were previously untested, so the
guard-before-work contract they rely on was unverified. A fake tree captures
each callback so it can be invoked directly without a Discord connection.
"""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.commands import build_registry
from src.commands.help_command import HelpCommand
from src.commands.leaderboard import LeaderboardCommand
from src.commands.ping import PingCommand
from src.commands.repped import ReppedCommand
from src.commands.setup import SetupCommand, SyncPromptView
from src.commands.status import StatusCommand
from src.commands.sync import SyncCommand
from src.core.command_manager import CommandManager
from src.services.guild_config import GuildConfig
from src.services.streaks import StreakStore
from src.services.tag_sync import GuildStats, SYNC_PAUSED_MESSAGE, TagSyncService


class FakeTree:
    """Collects the callbacks a command registers, keyed by slash name."""

    def __init__(self):
        self.commands = {}

    def command(self, *, name, description):
        def decorator(func):
            self.commands[name] = func
            return func
        return decorator


def register(command):
    tree = FakeTree()
    command.register(tree)
    return tree.commands


class Role:
    """Enough of discord.Role for the setup checks, including rank comparison."""

    def __init__(self, *, default=False, managed=False, position=1, administrator=False):
        self.id, self.mention = 10, "<@&10>"
        self.default, self.managed, self.position = default, managed, position
        self.permissions = SimpleNamespace(administrator=administrator)

    def is_default(self):
        return self.default

    def __ge__(self, position):
        # guild.me.top_role is an int here, so the reflected operand is used.
        return self.position >= position


@pytest.fixture
def interaction():
    return SimpleNamespace(
        guild_id=1,
        guild=SimpleNamespace(id=1, me=SimpleNamespace(
            top_role=10, guild_permissions=SimpleNamespace(manage_roles=True),
        )),
        # An authorised invoker, ranked above every role these tests build.
        user=SimpleNamespace(id=42, top_role=100, guild_permissions=SimpleNamespace(
            manage_guild=True, manage_roles=True, administrator=False,
        )),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock(),
                                 edit_message=AsyncMock(), is_done=lambda: False),
        followup=SimpleNamespace(send=AsyncMock()),
    )


@pytest.fixture
def config(monkeypatch):
    store = SimpleNamespace(get_role=AsyncMock(return_value="10"),
                            set_role=AsyncMock(), disable=AsyncMock(return_value=True))
    monkeypatch.setattr(GuildConfig, "_instance", store)
    return store


# --- slash wiring -----------------------------------------------------------

@pytest.mark.parametrize("command,slash,extra", [
    (SetupCommand(), "setup", (Role(),)),
    (SetupCommand(), "disable", ()),
    (StatusCommand(), "status", ()),
    (SyncCommand(), "sync", ()),
    (HelpCommand([PingCommand()]), "help", (None,)),
    (PingCommand(), "ping", ()),
    (LeaderboardCommand(), "leaderboard", (None,)),
    (ReppedCommand(), "repped", (None,)),
])
async def test_registered_callbacks_refuse_a_failed_guard(interaction, config, command, slash, extra):
    """Every slash callback must stop at the guard before touching state."""
    interaction.guild = None  # guild_only commands reject DMs
    command.guild_only = True
    await register(command)[slash](interaction, *extra)
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True
    config.set_role.assert_not_awaited()
    config.disable.assert_not_awaited()


async def test_setup_callback_configures_the_chosen_role(interaction, config):
    await register(SetupCommand())["setup"](interaction, Role())
    config.set_role.assert_awaited_once_with("1", "10")
    view = interaction.response.send_message.await_args.kwargs["view"]
    view.stop()


@pytest.mark.parametrize("was_configured,fragment", [
    (True, "Tag rewards disabled"),
    (False, "wasn't configured yet"),
])
async def test_disable_callback_reports_prior_state(interaction, config, was_configured, fragment):
    config.disable.return_value = was_configured
    await register(SetupCommand())["disable"](interaction)
    config.disable.assert_awaited_once_with("1")
    assert fragment in interaction.response.send_message.await_args.args[0]
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True


@pytest.mark.parametrize("command,slash", [(StatusCommand(), "status"), (SyncCommand(), "sync")])
async def test_registered_callbacks_run_once_the_guard_passes(interaction, config, monkeypatch, command, slash):
    executed = AsyncMock()
    monkeypatch.setattr(command, "execute", executed)
    await register(command)[slash](interaction)
    executed.assert_awaited_once_with(interaction)


async def test_ping_callback_responds(interaction, config):
    interaction.client = SimpleNamespace(latency=0.05)
    await register(PingCommand())["ping"](interaction)
    assert "Pong" in interaction.response.send_message.await_args.args[0]


@pytest.mark.parametrize("choice,expected", [
    (None, "current"),
    (SimpleNamespace(value="longest"), "longest"),
    (SimpleNamespace(value="total"), "total"),
])
async def test_leaderboard_callback_passes_the_sort_choice(interaction, config, monkeypatch, choice, expected):
    store = SimpleNamespace(leaderboard=AsyncMock(return_value=[]))
    monkeypatch.setattr(StreakStore, "_instance", store)
    await register(LeaderboardCommand())["leaderboard"](interaction, choice)
    store.leaderboard.assert_awaited_once_with("1", sort=expected, limit=10)


@pytest.mark.parametrize("target,expected_id", [(None, "42"), (SimpleNamespace(id=7, bot=False), "7")])
async def test_repped_callback_defaults_to_the_caller(interaction, config, monkeypatch, target, expected_id):
    interaction.user.bot = False
    interaction.user.display_name = "Tester"
    interaction.user.display_avatar = SimpleNamespace(url="https://example.invalid/a.png")
    if target is not None:
        target.display_name = "Other"
        target.display_avatar = SimpleNamespace(url="https://example.invalid/b.png")
    store = SimpleNamespace(get=AsyncMock(return_value={
        "current": 0, "longest": 0, "total": 0, "active": False,
    }))
    monkeypatch.setattr(StreakStore, "_instance", store)
    await register(ReppedCommand())["repped"](interaction, target)
    store.get.assert_awaited_once_with("1", expected_id)


async def test_setup_execute_points_at_the_role_argument(interaction):
    await SetupCommand().execute(interaction)
    assert "/setup role:@Role" in interaction.response.send_message.await_args.args[0]


# --- the post-setup sync prompt ---------------------------------------------

@pytest.fixture
def view():
    v = SyncPromptView("1", 42)  # 42 is the invoker fixture's id
    yield v
    v.stop()


async def test_sync_prompt_starts_a_sync_and_disables_itself(interaction, view, monkeypatch):
    service = SimpleNamespace(is_syncing=lambda gid: False, safe_full_sync=AsyncMock())
    monkeypatch.setattr(TagSyncService, "_instance", service)

    await view.sync_now.callback(interaction)
    await asyncio.sleep(0)  # let the background task start

    service.safe_full_sync.assert_awaited_once_with("1")
    message = interaction.response.edit_message.await_args.kwargs["content"]
    assert "Syncing your server tag rewards now" in message
    # The buttons are disabled so the prompt cannot be double-submitted.
    assert all(child.disabled for child in view.children)


async def test_sync_prompt_does_not_stack_a_second_sync(interaction, view, monkeypatch):
    service = SimpleNamespace(is_syncing=lambda gid: True, safe_full_sync=AsyncMock())
    monkeypatch.setattr(TagSyncService, "_instance", service)

    await view.sync_now.callback(interaction)
    await asyncio.sleep(0)

    service.safe_full_sync.assert_not_awaited()
    assert all(child.disabled for child in view.children)


async def test_sync_prompt_explains_limited_mode(interaction, view, monkeypatch):
    monkeypatch.setattr(TagSyncService, "_instance", None)
    await view.sync_now.callback(interaction)
    assert SYNC_PAUSED_MESSAGE in interaction.response.edit_message.await_args.kwargs["content"]


async def test_sync_prompt_later_leaves_sync_untouched(interaction, view, monkeypatch):
    service = SimpleNamespace(is_syncing=lambda gid: False, safe_full_sync=AsyncMock())
    monkeypatch.setattr(TagSyncService, "_instance", service)

    await view.later.callback(interaction)
    await asyncio.sleep(0)

    service.safe_full_sync.assert_not_awaited()
    assert "/sync anytime" in interaction.response.edit_message.await_args.kwargs["content"]
    assert all(child.disabled for child in view.children)


# --- /status ----------------------------------------------------------------

async def test_status_prompts_setup_when_unconfigured(interaction, config, monkeypatch):
    config.get_role.return_value = None
    service = SimpleNamespace(stats_for=lambda gid: GuildStats(), is_syncing=lambda gid: False)
    monkeypatch.setattr(TagSyncService, "_instance", service)

    await StatusCommand().execute(interaction)

    assert "/setup role:@Role" in interaction.response.send_message.await_args.args[0]
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True


@pytest.mark.parametrize("syncing,state", [(True, "Running"), (False, "Idle")])
async def test_status_renders_sync_statistics(interaction, config, monkeypatch, syncing, state):
    stats = GuildStats(members_checked=12, roles_added=3, roles_removed=1,
                       currently_repping=7, live_updates=5,
                       last_sync_at=datetime(2025, 1, 1, tzinfo=timezone.utc))
    monkeypatch.setattr(TagSyncService, "_instance", SimpleNamespace(
        stats_for=lambda gid: stats, is_syncing=lambda gid: syncing,
    ))

    await StatusCommand().execute(interaction)

    embed = interaction.response.send_message.await_args.kwargs["embed"]
    assert [field.value for field in embed.fields] == [
        "<@&10>", state, "12", "7", "3", "1", "5", "<t:1735689600:R>",
    ]
    assert embed.timestamp is not None


async def test_status_reports_a_sync_that_has_never_run(interaction, config, monkeypatch):
    monkeypatch.setattr(TagSyncService, "_instance", SimpleNamespace(
        stats_for=lambda gid: GuildStats(), is_syncing=lambda gid: False,
    ))
    await StatusCommand().execute(interaction)
    embed = interaction.response.send_message.await_args.kwargs["embed"]
    assert embed.fields[-1].value == "Not yet run"


# --- help restrictions ------------------------------------------------------

@pytest.mark.parametrize("guild_only,developer_only,expected", [
    (False, False, None),
    (True, False, "Server only"),
    (False, True, "Developer only"),
    (True, True, "Server only, Developer only"),
])
async def test_help_lists_command_restrictions(interaction, guild_only, developer_only, expected):
    target = PingCommand()
    target.guild_only, target.developer_only = guild_only, developer_only
    await HelpCommand([target])._show_specific(interaction, "ping")

    embed = interaction.response.send_message.await_args.kwargs["embed"]
    restrictions = [f.value for f in embed.fields if f.name == "⚠️ Restrictions"]
    assert restrictions == ([expected] if expected else [])


async def test_help_callback_routes_between_overview_and_detail(interaction):
    command = HelpCommand([PingCommand()])
    callback = register(command)["help"]

    await callback(interaction, None)
    assert interaction.response.send_message.await_args.kwargs["embed"].title.startswith("🤖")

    await callback(interaction, "ping")
    assert interaction.response.send_message.await_args.kwargs["embed"].title == "📖 Help: /ping"


# --- the command registry ---------------------------------------------------

@pytest.fixture
def manager():
    return CommandManager([PingCommand(), StatusCommand(), SetupCommand()])


def test_manager_exposes_commands_by_name_and_category(manager):
    assert manager.count() == 3
    assert [c.name for c in manager.all_commands()] == ["ping", "status", "setup"]
    assert manager.get("status").name == "status"
    assert manager.get("absent") is None
    assert {k: [c.name for c in v] for k, v in manager.by_category().items()} == {
        "Utility": ["ping", "status"], "Admin": ["setup"],
    }


def test_manager_all_commands_cannot_mutate_the_registry(manager):
    manager.all_commands().clear()
    assert manager.count() == 3


def test_manager_registers_every_command_once(manager):
    tree = FakeTree()
    manager.register_all(tree)
    # setup registers two slash commands from one Command object.
    assert sorted(tree.commands) == ["disable", "ping", "setup", "status"]


def test_build_registry_is_wired_and_unique():
    commands = build_registry()
    names = [c.name for c in commands]
    assert len(names) == len(set(names))

    tree = FakeTree()
    CommandManager(commands).register_all(tree)
    assert {"ping", "help", "setup", "disable", "status", "sync", "repped", "leaderboard"} <= set(tree.commands)
