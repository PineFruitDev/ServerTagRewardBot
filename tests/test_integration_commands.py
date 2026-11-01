"""Slash command flows, composed end to end.

test_admin_commands.py and test_display_commands.py already cover each command on its
own, with a fake tree and the services mocked out. These tests instead go through the
command tree main.build_bot really registers, against the real GuildConfig, StreakStore
and TagSyncService. That lets one test span two commands, or a command and a gateway
event: /setup then an equip, an equip then /repped, a sync then /status. Only the REST
layer and the store are fakes, and no socket is opened.
"""

import asyncio

import pytest
from discord import app_commands

from src.commands import setup as setup_command
from tests.integration_harness import (GUILD, ROLE, FakeGuild, FakeInteraction, FakeMember,
                                       FakeRole, button, field_values, member_payload, user_id)

ADMIN = user_id(1)
ALICE = user_id(2)
BOB = user_id(3)

# The embeds separate a name from a duration with an em dash, and use one on its
# own for a field with nothing to show. Spelled as escapes so these stay readable
# as the product strings they mirror rather than as prose.
SEP = "\u2014"
EMPTY = "\u2014"

#: A reward role the bot sits comfortably above
REWARD_ROLE = FakeRole(ROLE, position=5)


@pytest.fixture
def spawned(monkeypatch):
    """Capture the background tasks setup spawns so a test can await them."""
    tasks: list[asyncio.Task] = []
    real_spawn = setup_command.spawn

    def recording(coro, *, name):
        task = real_spawn(coro, name=name)
        tasks.append(task)
        return task

    monkeypatch.setattr(setup_command, "spawn", recording)
    return tasks


# --------------------------------------------------------------------------- #
# /setup
# --------------------------------------------------------------------------- #


async def test_setup_configures_the_reward_role(fresh_ui):
    interaction = await fresh_ui.run("setup", REWARD_ROLE)

    assert fresh_ui.store.guilds[GUILD] == {"role_id": ROLE, "enabled": True}
    assert REWARD_ROLE.mention in interaction.replied
    assert isinstance(interaction.view, setup_command.SyncPromptView)


async def test_setup_makes_the_guild_live_for_tag_detection(fresh_ui):
    """The whole point of /setup: events that did nothing before now grant the role."""
    await fresh_ui.equip(ALICE)
    assert not fresh_ui.has_reward(ALICE)

    await fresh_ui.run("setup", REWARD_ROLE)
    await fresh_ui.equip(ALICE)

    assert fresh_ui.has_reward(ALICE)


async def test_setup_sync_now_reconciles_the_existing_members(fresh_ui, spawned):
    """Accepting the first-sync prompt must catch members who equipped the tag earlier."""
    fresh_ui.http.seed_members(GUILD, [
        member_payload(ALICE, GUILD),
        member_payload(BOB, None),
    ])
    interaction = await fresh_ui.run("setup", REWARD_ROLE)

    click = FakeInteraction()
    await button(interaction.view, "Sync now").callback(click)
    await asyncio.gather(*spawned)

    assert fresh_ui.has_reward(ALICE)
    assert not fresh_ui.has_reward(BOB)
    assert fresh_ui.stats().members_checked == 2
    assert "Syncing" in click.response.edits[0]["content"]
    assert all(child.disabled for child in interaction.view.children)


# --------------------------------------------------------------------------- #
# /disable
# --------------------------------------------------------------------------- #


async def test_disable_stops_rewarding_and_leaves_roles_alone(ui):
    ui.http.grant(GUILD, ALICE, ROLE)

    interaction = await ui.run("disable")
    await ui.equip(BOB)

    assert ui.store.guilds[GUILD]["enabled"] is False
    assert "disabled" in interaction.replied
    assert ui.roles_of(ALICE) == {ROLE}  # existing roles are deliberately kept
    assert not ui.has_reward(BOB)


async def test_setup_can_re_enable_a_disabled_server(ui):
    await ui.run("disable")
    await ui.run("setup", REWARD_ROLE)
    await ui.equip(ALICE)

    assert ui.store.guilds[GUILD] == {"role_id": ROLE, "enabled": True}
    assert ui.has_reward(ALICE)


# --------------------------------------------------------------------------- #
# /sync
# --------------------------------------------------------------------------- #


async def test_sync_reports_what_it_reconciled(ui):
    ui.http.seed_members(GUILD, [
        member_payload(ALICE, GUILD),
        member_payload(BOB, None, roles=[ROLE]),
        member_payload(user_id(4), None),
    ])
    ui.http.grant(GUILD, BOB, ROLE)

    interaction = await ui.run("sync")

    assert interaction.response.deferred is True
    report = interaction.followed_up
    assert "Members checked:** 3" in report
    assert "Repping the tag:** 1" in report
    assert "Roles added:** 1" in report
    assert "Roles removed:** 1" in report
    assert ui.has_reward(ALICE) and not ui.has_reward(BOB)


async def test_sync_refuses_to_pile_up_on_a_running_sync(ui):
    entered, release = asyncio.Event(), asyncio.Event()
    ui.http.seed_members(GUILD, [member_payload(ALICE, GUILD)])
    real_get_members = ui.http.get_members

    async def gated(guild_id, *, limit, after):
        entered.set()
        await release.wait()
        return await real_get_members(guild_id, limit=limit, after=after)

    ui.http.get_members = gated
    first = asyncio.create_task(ui.service.full_sync(GUILD))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        interaction = await ui.run("sync")
    finally:
        release.set()
        await first

    assert "already in progress" in interaction.replied


# --------------------------------------------------------------------------- #
# /status
# --------------------------------------------------------------------------- #


async def test_status_reports_the_live_counters(ui):
    await ui.equip(ALICE)
    await ui.unequip(BOB, roles=[ROLE])

    fields = field_values((await ui.run("status")).embed)

    assert fields["Reward Role"] == f"<@&{ROLE}>"
    assert fields["Sync State"] == "Idle"
    assert fields["Roles Added"] == "1"
    assert fields["Roles Removed"] == "1"
    assert fields["Live Updates Seen"] == "2"
    assert fields["Last Full Sync"] == "Not yet run"


async def test_status_reports_totals_after_a_sync(ui):
    ui.http.seed_members(GUILD, [member_payload(ALICE, GUILD), member_payload(BOB, None)])
    await ui.run("sync")

    fields = field_values((await ui.run("status")).embed)

    assert fields["Members Checked"] == "2"
    assert fields["Repping the Tag"] == "1"
    assert fields["Last Full Sync"].startswith("<t:")


async def test_status_is_scoped_to_the_server_it_runs_in(ui):
    """One server's counters must never show up in another's /status."""
    other = "900000000000000007"
    ui.store.seed_guild(other, ROLE)
    await ui.equip(ALICE, guild_id=GUILD)

    here = field_values((await ui.run("status")).embed)
    there = field_values((await ui.run("status", interaction=FakeInteraction(FakeGuild(other)))).embed)

    assert here["Live Updates Seen"] == "1"
    assert there["Live Updates Seen"] == "0"


# --------------------------------------------------------------------------- #
# /repped
# --------------------------------------------------------------------------- #


async def test_repped_shows_a_running_streak(ui, clock):
    await ui.equip(ALICE)
    clock.advance(90061)  # 1d 1h 1m

    member = FakeMember(ALICE, name="Alice")
    embed = (await ui.run("repped", member)).embed
    fields = field_values(embed)

    assert "repping the tag for **1d 1h**" in embed.description
    assert fields["Current Streak"] == "1d 1h"
    assert fields["Longest Streak"] == "1d 1h"


async def test_repped_shows_a_lapsed_streak(ui, clock):
    await ui.equip(ALICE)
    clock.advance(7200)
    await ui.unequip(ALICE, roles=[ROLE])

    embed = (await ui.run("repped", FakeMember(ALICE, name="Alice"))).embed
    fields = field_values(embed)

    assert "isn't repping the tag right now" in embed.description
    assert fields["Current Streak"] == EMPTY
    assert fields["Longest Streak"] == "2h"
    assert fields["All-Time Repped"] == "2h"


async def test_repped_defaults_to_the_caller(ui, clock):
    await ui.equip(ADMIN)
    clock.advance(3600)

    interaction = FakeInteraction(user=FakeMember(ADMIN, name="Admin"))
    embed = (await ui.run("repped", interaction=interaction)).embed

    assert "Admin has been repping the tag for **1h**" in embed.description


# --------------------------------------------------------------------------- #
# /leaderboard
# --------------------------------------------------------------------------- #


async def test_leaderboard_ranks_the_longest_running_streaks(ui, clock):
    await ui.equip(ALICE)
    clock.advance(7200)
    await ui.equip(BOB)
    clock.advance(3600)

    embed = (await ui.run("leaderboard")).embed

    assert embed.description.splitlines() == [
        f"\U0001F947 <@{ALICE}> {SEP} **3h**",
        f"\U0001F948 <@{BOB}> {SEP} **1h**",
    ]
    assert "Current Streak" in embed.title


@pytest.mark.parametrize("sort,heading,winner", [
    ("current", "Current Streak", BOB),
    ("longest", "Longest Streak", ALICE),
    ("total", "All-Time Repped", ALICE),
])
async def test_leaderboard_honours_every_sort(ui, clock, sort, heading, winner):
    """Alice banked a long streak and stopped; Bob is mid-streak but behind on totals."""
    await ui.equip(ALICE)
    clock.advance(10800)
    await ui.unequip(ALICE, roles=[ROLE])
    await ui.equip(BOB)
    clock.advance(3600)

    choice = app_commands.Choice(name=heading.lower(), value=sort)
    embed = (await ui.run("leaderboard", choice)).embed

    assert heading in embed.title
    assert embed.description.splitlines()[0].startswith(f"🥇 <@{winner}>")


async def test_leaderboard_caps_at_ten_entries(ui, clock):
    for index in range(12):
        await ui.equip(user_id(20 + index))
        clock.advance(60)

    embed = (await ui.run("leaderboard")).embed

    assert len(embed.description.splitlines()) == 10


async def test_leaderboard_ignores_members_with_nothing_banked(ui, clock):
    """A member who equipped and unequipped in the same second has no streak to show."""
    await ui.equip(ALICE)
    await ui.unequip(ALICE, roles=[ROLE])

    interaction = await ui.run("leaderboard")

    assert "Nobody has repped the tag yet" in interaction.replied


# --------------------------------------------------------------------------- #
# Limited mode (Discord has not granted the Server Members intent)
# --------------------------------------------------------------------------- #


async def test_streak_commands_keep_working_without_the_intent(limited_ui):
    """Settings and history are unaffected by the missing intent."""
    limited_ui.store.streaks[(GUILD, ALICE)] = {
        "user_id": ALICE, "active_since": None, "total_seconds": 3600, "longest_seconds": 3600,
    }

    repped = await limited_ui.run("repped", FakeMember(ALICE, name="Alice"))
    board = await limited_ui.run("leaderboard", app_commands.Choice(name="all-time", value="total"))

    assert field_values(repped.embed)["All-Time Repped"] == "1h"
    assert f"<@{ALICE}>" in board.embed.description


# --------------------------------------------------------------------------- #
# The whole journey
# --------------------------------------------------------------------------- #


async def test_a_members_full_reward_lifecycle(fresh_ui, clock):
    """Setup, equip, earn, check, unequip, lose: the flow a real server lives through."""
    ui = fresh_ui

    # An admin configures the reward role
    await ui.run("setup", REWARD_ROLE)
    assert ui.store.guilds[GUILD]["role_id"] == ROLE

    # A member equips the server tag and is rewarded within the same event
    await ui.equip(ALICE)
    assert ui.has_reward(ALICE)

    # Two hours in, their streak is visible to themselves and on the board
    clock.advance(7200)
    assert "repping the tag for **2h**" in (await ui.run("repped", FakeMember(ALICE, name="Alice"))).embed.description
    assert f"\U0001F947 <@{ALICE}> {SEP} **2h**" in (await ui.run("leaderboard")).embed.description

    # They take the tag off; the role goes with it and the streak is banked
    await ui.unequip(ALICE, roles=[ROLE])
    assert not ui.has_reward(ALICE)
    assert await ui.streak(ALICE) == {"current": 0, "longest": 7200, "total": 7200, "active": False}

    # The history survives: still on the all-time board, no longer on the current one
    total_board = await ui.run("leaderboard", app_commands.Choice(name="all-time", value="total"))
    assert f"\U0001F947 <@{ALICE}> {SEP} **2h**" in total_board.embed.description
    assert "Nobody has repped the tag yet" in (await ui.run("leaderboard")).replied

    # And /status has counted both transitions
    fields = field_values((await ui.run("status")).embed)
    assert (fields["Roles Added"], fields["Roles Removed"], fields["Live Updates Seen"]) == ("1", "1", "2")
