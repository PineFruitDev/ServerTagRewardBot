"""Who is allowed to run the state-changing commands, and who is refused.

/setup, /disable and /sync change state: the first two write this server's
reward role config, the third walks every member and moves roles in bulk.
Discord's app_commands.default_permissions only sets the *default* a guild can
reassign (Server Settings, Integrations, Command Permissions), so these tests
drive the real registered tree callbacks and assert the bot re-checks the
invoker itself, refusing clearly and touching no state when it does.

The allowed cases matter as much as the denied ones: a gate that refuses
everybody would pass a denial-only suite.
"""

from types import SimpleNamespace

import discord
import pytest

from src.commands.setup import SetupCommand
from src.core.command import permission_label, user_permissions
from tests.integration_harness import (GUILD, ROLE, FakeGuild, FakeInteraction, FakeMember,
                                       FakeRole, user_id)

#: A reward role the bot sits comfortably above
REWARD_ROLE = FakeRole(ROLE, position=5)

NOBODY = discord.Permissions.none()
MANAGE_SERVER = discord.Permissions(manage_guild=True)
#: Administrator without manage_guild, so "any one of these" is what is tested
#: rather than a set that happens to satisfy both names.
ADMINISTRATOR = discord.Permissions(administrator=True)

#: The commands that change state, with the arguments their callbacks take
STATE_CHANGING = [("setup", (REWARD_ROLE,)), ("disable", ()), ("sync", ())]


def invoker(permissions: discord.Permissions, *, top_role: FakeRole | None = None) -> FakeMember:
    return FakeMember(user_id(1), name="Invoker", permissions=permissions, top_role=top_role)


def wrote_nothing(harness) -> bool:
    """No reward config was written and no member walk was started."""
    return harness.store.guilds == {} and "get_members" not in [c[0] for c in harness.http.calls]


# --------------------------------------------------------------------------- #
# The gate on the invoker
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("slash,extra", STATE_CHANGING)
async def test_a_member_without_manage_server_is_refused(fresh_ui, slash, extra):
    """The whole point: an ordinary member cannot reach any of these."""
    interaction = await fresh_ui.run(slash, *extra, interaction=FakeInteraction(user=invoker(NOBODY)))

    assert "You need" in interaction.replied
    assert wrote_nothing(fresh_ui)


@pytest.mark.parametrize("slash,extra", STATE_CHANGING)
@pytest.mark.parametrize("held", [MANAGE_SERVER, ADMINISTRATOR], ids=["manage_server", "administrator"])
async def test_manage_server_or_administrator_is_allowed_through(fresh_ui, slash, extra, held):
    """Either permission on its own is enough, and the command then runs."""
    interaction = await fresh_ui.run(slash, *extra, interaction=FakeInteraction(user=invoker(held)))

    assert "You need" not in interaction.replied
    if slash == "setup":
        # The one that writes, so it proves the run happened rather than
        # merely that it was not refused.
        assert fresh_ui.store.guilds[GUILD] == {"role_id": ROLE, "enabled": True}
        interaction.view.stop()
    else:
        # Neither /disable nor /sync has anything to do in a guild that was
        # never set up, and each says so: a reply only a cleared gate reaches.
        assert "isn't set up yet" in interaction.replied or "wasn't configured yet" in interaction.replied


@pytest.mark.parametrize("slash,extra", STATE_CHANGING)
async def test_a_denial_names_both_permissions_the_way_discord_labels_them(fresh_ui, slash, extra):
    interaction = await fresh_ui.run(slash, *extra, interaction=FakeInteraction(user=invoker(NOBODY)))

    assert "Manage Server" in interaction.replied
    assert "Administrator" in interaction.replied
    # The API's own spelling would send an admin hunting for a setting that is
    # not labelled that anywhere in Discord.
    assert "manage_guild" not in interaction.replied


@pytest.mark.parametrize("slash,extra", STATE_CHANGING)
async def test_a_denial_is_ephemeral(fresh_ui, slash, extra):
    """A refusal is nobody else's business, and should not sit in the channel."""
    interaction = await fresh_ui.run(slash, *extra, interaction=FakeInteraction(user=invoker(NOBODY)))

    assert interaction.response.messages[0]["ephemeral"] is True


@pytest.mark.parametrize("slash,extra", STATE_CHANGING)
async def test_a_denial_names_the_command_that_was_actually_run(fresh_ui, slash, extra):
    """/disable is registered by SetupCommand, so help_info.name would say /setup."""
    interaction = await fresh_ui.run(slash, *extra, interaction=FakeInteraction(user=invoker(NOBODY)))

    assert f"/{slash}" in interaction.replied


async def test_authorisation_is_checked_before_the_bots_own_permissions(fresh_ui):
    """An unauthorised member gets told that, not handed the bot's problems."""
    blind_bot = FakeGuild(me=FakeMember(user_id(99), name="TagBot", permissions=NOBODY))
    interaction = await fresh_ui.run("sync", interaction=FakeInteraction(
        guild=blind_bot, user=invoker(NOBODY),
    ))

    assert "You need" in interaction.replied
    assert "manage_roles" not in interaction.replied


# --------------------------------------------------------------------------- #
# Reading the invoker's permissions
# --------------------------------------------------------------------------- #


def test_a_guildless_interaction_grants_the_invoker_nothing():
    assert user_permissions(SimpleNamespace(guild=None)) is None


def test_permissions_fall_back_to_the_resolved_interaction_set():
    """interaction.user is a User, not a Member, when no member object was sent."""
    resolved = discord.Permissions(manage_guild=True)
    held = user_permissions(SimpleNamespace(
        guild=SimpleNamespace(id=1), user=SimpleNamespace(id=42), permissions=resolved,
    ))
    assert held is resolved


def test_unreadable_invoker_permissions_are_reported_not_guessed():
    """Neither a Member nor a resolved set: refuse rather than assume either way."""
    assert user_permissions(SimpleNamespace(guild=SimpleNamespace(id=1),
                                            user=SimpleNamespace(id=42))) is None


async def test_an_invoker_whose_permissions_cannot_be_read_is_refused(fresh_ui):
    interaction = FakeInteraction()
    # No guild_permissions, and FakeInteraction carries no resolved set either
    interaction.user = SimpleNamespace(id=42)

    await fresh_ui.run("setup", REWARD_ROLE, interaction=interaction)

    assert "can't read your permissions" in interaction.replied
    assert wrote_nothing(fresh_ui)


def test_an_unlabelled_permission_still_reads_as_prose():
    assert permission_label("manage_guild") == "Manage Server"
    assert permission_label("moderate_members") == "Moderate Members"


# --------------------------------------------------------------------------- #
# /setup cannot be used to climb
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("held", [MANAGE_SERVER, ADMINISTRATOR], ids=["manage_server", "administrator"])
async def test_an_administrator_role_is_never_the_reward(fresh_ui, held):
    """The reward lands on everyone wearing the tag, so this one is absolute."""
    admin_role = FakeRole(ROLE, position=5, permissions=discord.Permissions(administrator=True))
    interaction = await fresh_ui.run("setup", admin_role, interaction=FakeInteraction(
        user=invoker(held, top_role=FakeRole("800000000000000009", position=49)),
    ))

    assert "Administrator" in interaction.replied
    assert wrote_nothing(fresh_ui)


@pytest.mark.parametrize("rank,allowed", [(3, False), (5, False), (10, True)],
                         ids=["below_the_reward", "level_with_it", "above_it"])
async def test_setup_mirrors_discords_rule_on_handing_out_a_role(fresh_ui, rank, allowed):
    """Manage Server does not confer Manage Roles.

    Without this, a member holding only Manage Server could name a role above
    their own rank as the reward, equip the tag and collect it.
    """
    interaction = await fresh_ui.run("setup", REWARD_ROLE, interaction=FakeInteraction(
        user=invoker(MANAGE_SERVER, top_role=FakeRole("800000000000000009", position=rank)),
    ))

    if allowed:
        assert fresh_ui.store.guilds[GUILD] == {"role_id": ROLE, "enabled": True}
        interaction.view.stop()
    else:
        assert "not below your own highest role" in interaction.replied
        assert wrote_nothing(fresh_ui)


async def test_an_administrator_outranks_the_hierarchy_check(fresh_ui):
    """Administrator resolves to every permission, so rank cannot block them."""
    interaction = await fresh_ui.run("setup", REWARD_ROLE, interaction=FakeInteraction(
        user=invoker(ADMINISTRATOR, top_role=FakeRole("800000000000000009", position=1)),
    ))

    assert fresh_ui.store.guilds[GUILD] == {"role_id": ROLE, "enabled": True}
    interaction.view.stop()


async def test_a_direct_configure_call_still_refuses_an_overranked_role():
    """_configure must not depend on a caller it cannot see."""
    interaction = FakeInteraction(user=invoker(
        MANAGE_SERVER, top_role=FakeRole("800000000000000009", position=1),
    ))
    await SetupCommand()._configure(interaction, REWARD_ROLE)

    assert "not below your own highest role" in interaction.replied
