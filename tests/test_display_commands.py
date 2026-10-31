"""Offline coverage of user-facing commands and reward history."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.commands.help_command import HelpCommand, sanitize_name
from src.commands.leaderboard import LeaderboardCommand
from src.commands.ping import PingCommand
from src.commands.repped import ReppedCommand
from src.services.guild_config import GuildConfig
from src.services.streaks import StreakStore


@pytest.fixture
def context(monkeypatch):
    interaction = SimpleNamespace(
        guild_id=1, client=SimpleNamespace(latency=0.1234),
        user=SimpleNamespace(id=42, bot=False, display_name="Tester",
                             display_avatar=SimpleNamespace(url="https://example.invalid/avatar.png")),
        response=SimpleNamespace(send_message=AsyncMock()),
    )
    config = SimpleNamespace(get_role=AsyncMock(return_value="10"))
    store = SimpleNamespace(get=AsyncMock(), leaderboard=AsyncMock(return_value=[]))
    monkeypatch.setattr(GuildConfig, "_instance", config)
    monkeypatch.setattr(StreakStore, "_instance", store)
    return interaction, config, store


async def test_ping_reports_milliseconds(context):
    interaction, _, _ = context
    await PingCommand().execute(interaction)
    assert "123ms" in interaction.response.send_message.await_args.args[0]


@pytest.mark.parametrize("command", [ReppedCommand, LeaderboardCommand])
async def test_unconfigured_commands_do_not_read_streaks(context, command):
    interaction, config, store = context
    config.get_role.return_value = None
    await command().execute(interaction)
    assert "/setup" in interaction.response.send_message.await_args.args[0]
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True
    store.get.assert_not_awaited()
    store.leaderboard.assert_not_awaited()


async def test_repped_rejects_bots(context):
    interaction, _, store = context
    interaction.user.bot = True
    await ReppedCommand().execute(interaction)
    store.get.assert_not_awaited()
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True


@pytest.mark.parametrize("active,total,headline", [
    (True, 180, "has been repping"),
    (False, 180, "isn't repping"),
    (False, 0, "hasn't repped"),
])
async def test_repped_displays_history(context, active, total, headline):
    interaction, _, store = context
    store.get.return_value = dict(active=active, current=60 if active else 0,
                                  longest=120 if total else 0, total=total)
    await ReppedCommand().execute(interaction)
    store.get.assert_awaited_once_with("1", "42")
    embed = interaction.response.send_message.await_args.kwargs["embed"]
    assert headline in embed.description
    assert [f.value for f in embed.fields] == [
        "1m" if active else "—", "2m" if total else "—", "3m" if total else "—",
    ]


@pytest.mark.parametrize("sort,label", [("current", "Current Streak"),
                                        ("longest", "Longest Streak"), ("total", "All-Time Repped")])
async def test_leaderboard_renders_rank_and_sort(context, sort, label):
    interaction, _, store = context
    store.leaderboard.return_value = [(str(i), 60 * (5 - i)) for i in range(4)]
    await LeaderboardCommand()._show(interaction, sort)
    store.leaderboard.assert_awaited_once_with("1", sort=sort, limit=10)
    embed = interaction.response.send_message.await_args.kwargs["embed"]
    assert label in embed.title
    assert embed.description.splitlines() == [
        "🥇 <@0> — **5m**", "🥈 <@1> — **4m**", "🥉 <@2> — **3m**", "`#4` <@3> — **2m**",
    ]


async def test_empty_leaderboard(context):
    interaction, _, _ = context
    await LeaderboardCommand().execute(interaction)
    assert "Nobody" in interaction.response.send_message.await_args.args[0]


@pytest.mark.parametrize("sort", ["bogus", "", "CURRENT", "Longest"])
async def test_leaderboard_falls_back_for_an_unrecognised_sort(context, sort):
    """streaks.rank() tolerates an unknown sort, so the heading must too."""
    interaction, _, store = context
    store.leaderboard.return_value = [("1", 120)]
    await LeaderboardCommand()._show(interaction, sort)
    # Heading and ordering have to agree on the sort actually used.
    store.leaderboard.assert_awaited_once_with("1", sort="current", limit=10)
    assert "Current Streak" in interaction.response.send_message.await_args.kwargs["embed"].title


async def test_help_lists_categories_and_case_insensitive_details(context):
    interaction, _, _ = context
    command = HelpCommand([PingCommand(), ReppedCommand()])
    await command.execute(interaction)
    embed = interaction.response.send_message.await_args.kwargs["embed"]
    assert [f.name for f in embed.fields] == ["📂 Utility", "📂 Streaks"]
    await command._show_specific(interaction, "PING")
    embed = interaction.response.send_message.await_args.kwargs["embed"]
    assert embed.title == "📖 Help: /ping"
    assert embed.fields[0].value == "`/ping`"
    await command._show_specific(interaction, "missing")
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True
    assert "not found" in interaction.response.send_message.await_args.args[0]


# --- echoing back an unknown command name -----------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("ping", "ping"),
    ("  PING  ", "PING"),
    ("a b   c", "a b c"),
    ("", "(blank)"),
    ("   ", "(blank)"),
    ("**bold**", r"\*\*bold\*\*"),
    ("_x_", r"\_x\_"),
])
def test_sanitize_name_collapses_and_escapes(raw, expected):
    assert sanitize_name(raw) == expected


def test_sanitize_name_clamps_oversized_input():
    assert sanitize_name("A" * 6000) == "A" * 60 + "…"


async def test_help_detail_lookup_tolerates_surrounding_space(context):
    interaction, _, _ = context
    await HelpCommand([PingCommand()])._show_specific(interaction, "  Ping ")
    assert interaction.response.send_message.await_args.kwargs["embed"].title == "📖 Help: /ping"


async def test_unknown_command_reply_stays_inside_discords_limit(context):
    """A string option holds 6000 characters; a message body holds 2000."""
    interaction, _, _ = context
    await HelpCommand([PingCommand()])._show_specific(interaction, "A" * 6000)
    assert len(interaction.response.send_message.await_args.args[0]) < 2000


async def test_unknown_command_reply_cannot_ping_or_format(context):
    interaction, _, _ = context
    await HelpCommand([PingCommand()])._show_specific(interaction, "@everyone **loud**")
    sent = interaction.response.send_message.await_args
    assert r"\*\*loud\*\*" in sent.args[0]
    mentions = sent.kwargs["allowed_mentions"]
    assert (mentions.everyone, mentions.users, mentions.roles) == (False, False, False)
    assert sent.kwargs["ephemeral"] is True
