"""Offline coverage of admin commands, slash wiring, and the command registry.

The slash callbacks built in register() were previously untested, so the
guard-before-work contract they rely on was unverified. A fake tree captures
each callback so it can be invoked directly without a Discord connection.
"""

import asyncio
import io
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from src.commands import build_registry
from src.commands.help_command import HelpCommand
from src.commands.leaderboard import LeaderboardCommand
from src.commands.ping import LATENCY_PENDING, PingCommand
from src.commands.repped import ReppedCommand
from src.commands.setup import SetupCommand, SyncPromptView
from src.commands import status as status_module
from src.commands.status import StatusCommand
from src.commands.sync import SyncCommand
from src.core import view as view_module
from src.core.command_manager import CommandManager
from src.core.view import GuardedView
from src.services.environment import Environment
from src.services.guild_config import GuildConfig
from src.services.streaks import StreakStore
from src.services.tag_sync import GuildStats, SYNC_PAUSED_MESSAGE, TagSyncService
from tests.integration_harness import config_for_tests


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


@pytest.fixture(autouse=True)
def no_rate_limits(monkeypatch):
    """This file is about slash wiring, and some of it calls one callback twice.

    The limits themselves are tested in tests/test_cooldowns.py, including
    through these same registered callbacks.
    """
    monkeypatch.setattr(Environment, "_config", config_for_tests())


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


@pytest.mark.parametrize("latency,expected", [
    (0.05, "50ms"),
    (0.0, "0ms"),
    # A measurement taken across a clock adjustment, shown as 0 rather than -3ms.
    (-0.003, "0ms"),
    # discord.py's own two "no figure yet" values. KeepAliveHandler starts its
    # latency at inf and DiscordWebSocket.latency returns inf while that handler
    # does not exist, so every connect and resume has a window of up to one
    # heartbeat interval here; Client.latency returns nan while there is no
    # websocket to ask at all. round() raises on both.
    (float("inf"), LATENCY_PENDING),
    (float("nan"), LATENCY_PENDING),
])
async def test_ping_reports_every_latency_the_gateway_can_report(interaction, config,
                                                                 latency, expected):
    """/ping must answer during the window before the first heartbeat ack.

    It is the command people run when they are unsure the bot is up, which is
    exactly when the latency figure is missing. Raising there told the user "an
    error occurred" and logged a defect, about a bot that was working.
    """
    interaction.client = SimpleNamespace(latency=latency)
    await register(PingCommand())["ping"](interaction)
    reply = interaction.response.send_message.await_args.args[0]
    assert "Pong" in reply
    assert f"**API Latency:** {expected}" in reply


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


async def test_sync_prompt_disables_its_buttons_among_other_items(interaction, view):
    """The base Item type has no disabled flag, so the loop has to pick buttons out."""
    view.add_item(discord.ui.Select(options=[discord.SelectOption(label="x")]))
    await view.later.callback(interaction)
    buttons = [c for c in view.children if isinstance(c, discord.ui.Button)]
    assert len(buttons) == 2 and all(b.disabled for b in buttons)


async def test_sync_prompt_later_leaves_sync_untouched(interaction, view, monkeypatch):
    service = SimpleNamespace(is_syncing=lambda gid: False, safe_full_sync=AsyncMock())
    monkeypatch.setattr(TagSyncService, "_instance", service)

    await view.later.callback(interaction)
    await asyncio.sleep(0)

    service.safe_full_sync.assert_not_awaited()
    assert "/sync anytime" in interaction.response.edit_message.await_args.kwargs["content"]
    assert all(child.disabled for child in view.children)


# --- the prompt expiring unanswered -----------------------------------------

@pytest.fixture
def view_log(monkeypatch):
    """Capture what src/core/view.py logs. Its logger does not propagate."""
    stream = io.StringIO()
    monkeypatch.setattr(view_module.log, "handlers", [logging.StreamHandler(stream)])
    return stream


async def test_an_expired_sync_prompt_retires_itself(interaction):
    """discord.py drops a timed-out view, so its buttons stop being dispatched.

    They keep rendering as enabled until the message is edited, and an
    undispatched press is never answered, so an admin who stepped away from
    /setup for five minutes came back to a live-looking prompt that answered
    with Discord's own "This interaction failed" and never mentioned /sync.
    """
    interaction.edit_original_response = AsyncMock()
    view = SyncPromptView("1", 42, origin=interaction)
    try:
        await view.on_timeout()
    finally:
        view.stop()

    edited = interaction.edit_original_response.await_args.kwargs
    # Says setup is saved (the confirmation it writes over) and where the offer
    # it is retiring can still be taken up.
    assert "/sync" in edited["content"] and "saved" in edited["content"]
    # Sent back with the prompt, so the buttons stop inviting a press that
    # cannot be served.
    assert edited["view"] is view
    assert all(child.disabled for child in view.children)


async def test_the_library_default_no_longer_handles_an_expired_prompt():
    """discord.ui.View.on_timeout does nothing, which is what this replaces."""
    assert SyncPromptView.on_timeout is GuardedView.on_timeout
    assert GuardedView.on_timeout is not discord.ui.View.on_timeout


async def test_an_expired_prompt_without_its_interaction_still_closes_quietly():
    """A view built without an origin has no message to edit, and must not raise.

    on_timeout runs in a bare task of discord.py's own making, so anything
    raised here is lost to the library's logger rather than handled.
    """
    view = GuardedView(timeout=300)
    view.add_item(discord.ui.Button(label="x"))
    try:
        await view.on_timeout()
    finally:
        view.stop()
    assert all(child.disabled for child in view.children)


async def test_a_prompt_that_cannot_be_edited_is_reported_and_not_raised(interaction, view_log):
    """The reply can have been dismissed, and Discord can refuse the edit."""
    interaction.edit_original_response = AsyncMock(
        side_effect=discord.DiscordException("message is gone"))
    view = SyncPromptView("1", 42, origin=interaction)
    try:
        await view.on_timeout()
    finally:
        view.stop()
    assert "/setup could not retire its expired prompt" in view_log.getvalue()


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
                       currently_repping=7, live_updates=5, duplicates_collapsed=2,
                       last_sync_at=datetime(2025, 1, 1, tzinfo=timezone.utc))
    monkeypatch.setattr(TagSyncService, "_instance", SimpleNamespace(
        stats_for=lambda gid: stats, is_syncing=lambda gid: syncing,
    ))

    await StatusCommand().execute(interaction)

    embed = interaction.response.send_message.await_args.kwargs["embed"]
    assert [field.value for field in embed.fields] == [
        "<@&10>", state, "12", "7", "3", "1", "5", "2", "<t:1735689600:R>",
    ]
    assert embed.timestamp is not None


async def test_status_reports_a_sync_that_has_never_run(interaction, config, monkeypatch):
    monkeypatch.setattr(TagSyncService, "_instance", SimpleNamespace(
        stats_for=lambda gid: GuildStats(), is_syncing=lambda gid: False,
    ))
    await StatusCommand().execute(interaction)
    embed = interaction.response.send_message.await_args.kwargs["embed"]
    assert embed.fields[-1].value == "Not yet run"


async def test_status_reports_a_reward_role_it_cannot_manage(interaction, config, monkeypatch):
    """The live path records this from the first equipped tag, before any sync.

    So the counts are all zero and "Last Full Sync" still reads "Not yet run":
    without the warning the embed describes a server where nothing has happened
    yet rather than one where every grant is being refused, and /sync is the
    only other command that would say so.
    """
    stats = GuildStats(live_updates=1, role_blocked="HTTP 403 (Discord error 50013)")
    monkeypatch.setattr(TagSyncService, "_instance", SimpleNamespace(
        stats_for=lambda gid: stats, is_syncing=lambda gid: False,
    ))

    await StatusCommand().execute(interaction)

    embed = interaction.response.send_message.await_args.kwargs["embed"]
    # First field, so it is read before the counts it explains.
    assert "⚠️" in embed.fields[0].name
    assert "HTTP 403" in embed.fields[0].value
    assert "Manage Roles" in embed.fields[0].value
    assert embed.fields[0].inline is False
    assert embed.color.value == status_module.BLOCKED_COLOR
    # The statistics are still reported alongside it.
    assert embed.fields[-1].value == "Not yet run"


async def test_status_reports_a_stall_as_temporary_not_as_a_misconfiguration(
        interaction, config, monkeypatch):
    """A rate limit is not something the admin can fix in Server Settings.

    Wording it like role_blocked would send them to change permissions that
    were never the problem, exactly as /sync is careful not to.
    """
    stats = GuildStats(members_checked=12, roles_added=3, stalled_after=5)
    monkeypatch.setattr(TagSyncService, "_instance", SimpleNamespace(
        stats_for=lambda gid: stats, is_syncing=lambda gid: False,
    ))

    await StatusCommand().execute(interaction)

    embed = interaction.response.send_message.await_args.kwargs["embed"]
    assert "⚠️" in embed.fields[0].name
    assert "5" in embed.fields[0].value and "again" in embed.fields[0].value
    assert "Manage Roles" not in embed.fields[0].value
    assert embed.color.value == status_module.STALLED_COLOR


async def test_status_adds_no_warning_when_nothing_is_wrong(interaction, config, monkeypatch):
    """The healthy embed keeps its shape: nine fields, blurple, no warning."""
    monkeypatch.setattr(TagSyncService, "_instance", SimpleNamespace(
        stats_for=lambda gid: GuildStats(members_checked=12), is_syncing=lambda gid: False,
    ))

    await StatusCommand().execute(interaction)

    embed = interaction.response.send_message.await_args.kwargs["embed"]
    assert len(embed.fields) == 9
    assert embed.color.value == status_module.OK_COLOR
    assert all("⚠️" not in field.name for field in embed.fields)


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


async def test_help_covers_every_slash_command_the_registry_registers(interaction):
    """A Command can register more than one slash command; help must list each.

    SetupCommand registers /disable alongside /setup, so help built from
    help_info alone omitted /disable from the overview and told anyone who
    asked for it that no such command existed.
    """
    commands = build_registry()
    tree = FakeTree()
    CommandManager(commands).register_all(tree)

    await HelpCommand(commands).execute(interaction)
    embed = interaction.response.send_message.await_args.kwargs["embed"]
    listed = {line.split("`")[1].lstrip("/")
              for field in embed.fields for line in field.value.splitlines()}
    assert listed == set(tree.commands)

    for slash in tree.commands:
        await HelpCommand(commands)._show_specific(interaction, slash)
        reply = interaction.response.send_message.await_args
        assert reply.kwargs.get("embed") is not None, f"/{slash} has no help entry"
        assert reply.kwargs["embed"].title == f"📖 Help: /{slash}"


async def test_help_detail_for_disable_carries_setups_own_restrictions(interaction):
    """The gates belong to the Command, so both its slash commands show them."""
    await HelpCommand([SetupCommand()])._show_specific(interaction, "disable")
    embed = interaction.response.send_message.await_args.kwargs["embed"]
    assert embed.fields[0].value == "`/disable`"
    assert [f.value for f in embed.fields if f.name == "⚠️ Restrictions"] == ["Server only"]


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
