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

from src.commands.setup import SetupCommand, SyncPromptView
from src.commands.sync import SyncCommand
from src.core.command import permission_label, user_permissions
from tests.integration_harness import (GUILD, ROLE, FakeGuild, FakeInteraction, FakeMember,
                                       FakeRole, user_id)

#: A reward role the bot sits comfortably above
REWARD_ROLE = FakeRole(ROLE, position=5)

NOBODY = discord.Permissions.none()
#: Enough to run the commands, but conferring no role control at all
MANAGE_SERVER = discord.Permissions(manage_guild=True)
#: Manage Server plus the Manage Roles needed to hand a role out
SERVER_ADMIN = discord.Permissions(manage_guild=True, manage_roles=True)
#: Administrator with no other bit set, which FakeMember resolves to every
#: permission the way Member.guild_permissions does. Holds manage_guild only by
#: that implication, so it also proves "any one of these" on the run gate.
ADMINISTRATOR = discord.Permissions(administrator=True)

#: The commands that change state, with the arguments their callbacks take
STATE_CHANGING = [("setup", (REWARD_ROLE,)), ("disable", ()), ("sync", ())]

#: Only the run gate phrases its denial this way, so asserting on it does not
#: accidentally match the separate refusals about which role may be handed out.
RUN_GATE = "to use /"


def invoker(permissions: discord.Permissions, *, top_role: FakeRole | None = None) -> FakeMember:
    return FakeMember(user_id(1), name="Invoker", permissions=permissions, top_role=top_role)


def rank(position: int, *, role_id: str = "800000000000000009") -> FakeRole:
    return FakeRole(role_id, position=position, name="Invoker top role")


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
@pytest.mark.parametrize("held", [SERVER_ADMIN, ADMINISTRATOR], ids=["manage_server", "administrator"])
async def test_an_authorised_admin_is_allowed_through(fresh_ui, slash, extra, held):
    """Manage Server and Administrator each clear every gate on their own."""
    interaction = await fresh_ui.run(slash, *extra, interaction=FakeInteraction(user=invoker(held)))

    assert "❌" not in interaction.replied
    if slash == "setup":
        # The one that writes, so it proves the run happened rather than
        # merely that it was not refused.
        assert fresh_ui.store.guilds[GUILD] == {"role_id": ROLE, "enabled": True}
        interaction.view.stop()
    else:
        # Neither /disable nor /sync has anything to do in a guild that was
        # never set up, and each says so: a reply only a cleared gate reaches.
        assert "isn't set up yet" in interaction.replied or "wasn't configured yet" in interaction.replied


@pytest.mark.parametrize("slash,extra", [("disable", ()), ("sync", ())])
async def test_manage_server_alone_runs_what_hands_out_no_role(fresh_ui, slash, extra):
    """The run gate asks for Manage Server, and Manage Server is enough for it."""
    interaction = await fresh_ui.run(slash, *extra, interaction=FakeInteraction(
        user=invoker(MANAGE_SERVER),
    ))

    assert RUN_GATE not in interaction.replied


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


def test_permissions_come_from_the_member_and_nowhere_else():
    """Interaction.permissions is deliberately not a fallback.

    discord.py only populates it from the same member object that gives the
    invoker guild_permissions, so it is never the one source available, and it
    is channel-scoped rather than the guild-wide set this compares against.
    """
    assert user_permissions(SimpleNamespace(
        guild=SimpleNamespace(id=1), user=SimpleNamespace(id=42),
        permissions=discord.Permissions.all(),
    )) is None


def test_unreadable_invoker_permissions_are_reported_not_guessed():
    """No member object: refuse rather than assume either way."""
    assert user_permissions(SimpleNamespace(guild=SimpleNamespace(id=1),
                                            user=SimpleNamespace(id=42))) is None


async def test_an_invoker_whose_permissions_cannot_be_read_is_refused(fresh_ui):
    interaction = FakeInteraction()
    # No guild_permissions, and FakeInteraction carries no resolved set either
    interaction.user = SimpleNamespace(id=42)

    await fresh_ui.run("setup", REWARD_ROLE, interaction=interaction)

    assert "can't read your permissions" in interaction.replied
    assert wrote_nothing(fresh_ui)


async def test_a_command_asking_for_guild_permissions_refuses_a_dm():
    """Guild permissions mean nothing in a DM, so there is nothing to check.

    guild_only already rejects DMs for all three of today's commands, so this
    drives validate() directly. A future command that set
    required_user_permissions and forgot guild_only would otherwise reach its
    work in a DM with the invoker gate skipped entirely.
    """
    command = SyncCommand()
    command.guild_only = False
    valid, reason = await command.validate(FakeInteraction(dm=True))

    assert valid is False
    assert "only be used in servers" in reason


def test_an_unlabelled_permission_still_reads_as_prose():
    assert permission_label("manage_guild") == "Manage Server"
    assert permission_label("moderate_members") == "Moderate Members"


# --------------------------------------------------------------------------- #
# /setup cannot be used to climb
# --------------------------------------------------------------------------- #


OWNER = user_id(1)  # matches invoker(), so FakeGuild(owner_id=OWNER) owns them


@pytest.mark.parametrize("held,owned", [(SERVER_ADMIN, False), (ADMINISTRATOR, False),
                                        (ADMINISTRATOR, True)],
                         ids=["server_admin", "administrator", "the_owner"])
async def test_an_administrator_role_is_never_the_reward(fresh_ui, held, owned):
    """The reward lands on everyone wearing the tag, so this one is absolute.

    Checked ahead of the owner exemption, so not even the owner can turn
    equipping a server tag into a route to Administrator.
    """
    admin_role = FakeRole(ROLE, position=5, permissions=discord.Permissions(administrator=True))
    interaction = await fresh_ui.run("setup", admin_role, interaction=FakeInteraction(
        guild=FakeGuild(owner_id=OWNER if owned else None),
        user=invoker(held, top_role=rank(49)),
    ))

    assert "won't hand it out" in interaction.replied
    assert wrote_nothing(fresh_ui)


async def test_manage_server_alone_cannot_pick_the_reward_role(fresh_ui):
    """Manage Server confers no role control, so it cannot hand a role out.

    This is the climb the gate exists to stop: Manage Server is not Manage
    Roles, so on its own it must not be able to name a privileged role as the
    reward, equip the tag and collect it.
    """
    interaction = await fresh_ui.run("setup", REWARD_ROLE, interaction=FakeInteraction(
        user=invoker(MANAGE_SERVER, top_role=rank(40)),
    ))

    assert "Manage Roles" in interaction.replied
    assert wrote_nothing(fresh_ui)


# At equal position discord.Role ranks the LOWER id higher, so the invoker's
# own role id decides the tie. REWARD_ROLE's id is ROLE.
OLDER_THAN_REWARD = str(int(ROLE) - 1)
NEWER_THAN_REWARD = str(int(ROLE) + 1)


@pytest.mark.parametrize("top,allowed", [
    (rank(3), False),
    (rank(5, role_id=NEWER_THAN_REWARD), False),
    (rank(5, role_id=OLDER_THAN_REWARD), True),
    (rank(10), True),
], ids=["below_the_reward", "tied_and_newer", "tied_and_older", "above_it"])
async def test_the_reward_must_sit_below_the_invokers_own_highest_role(fresh_ui, top, allowed):
    """Discord's rank half, tie-break included."""
    interaction = await fresh_ui.run("setup", REWARD_ROLE, interaction=FakeInteraction(
        user=invoker(SERVER_ADMIN, top_role=top),
    ))

    if allowed:
        assert fresh_ui.store.guilds[GUILD] == {"role_id": ROLE, "enabled": True}
        interaction.view.stop()
    else:
        assert "not below your own highest role" in interaction.replied
        assert wrote_nothing(fresh_ui)


async def test_an_administrator_is_still_bound_by_the_hierarchy(fresh_ui):
    """Administrator grants every permission but lifts nobody over the ranks.

    Discord lets only the owner assign a role above their own highest, so
    exempting Administrator here would reopen the climb one rung further up:
    an admin ranked below a privileged role could still make it the reward and
    then collect it by equipping the tag.
    """
    interaction = await fresh_ui.run("setup", REWARD_ROLE, interaction=FakeInteraction(
        user=invoker(ADMINISTRATOR, top_role=rank(1)),
    ))

    assert "not below your own highest role" in interaction.replied
    assert wrote_nothing(fresh_ui)


async def test_the_owner_is_the_one_member_above_the_hierarchy(fresh_ui):
    """The guild owner can hand out any role, which is Discord's own rule.

    The same invoker, rank and role as the Administrator case above, which is
    refused. Ownership is the only difference, so it is what is under test.
    """
    interaction = await fresh_ui.run("setup", REWARD_ROLE, interaction=FakeInteraction(
        guild=FakeGuild(owner_id=OWNER),
        # An owner's guild_permissions is all-True in discord.py regardless of
        # their roles, so a powerless owner is not a shape production produces.
        user=invoker(ADMINISTRATOR, top_role=rank(1)),
    ))

    assert fresh_ui.store.guilds[GUILD] == {"role_id": ROLE, "enabled": True}
    interaction.view.stop()


async def test_a_direct_configure_call_refuses_what_it_cannot_check():
    """_configure must not depend on a caller it cannot see.

    Both reads it needs can be absent, and absent has to mean refuse rather
    than fall through to the write.
    """
    interaction = FakeInteraction()
    interaction.user = SimpleNamespace(id=42)  # no guild_permissions, no top_role
    await SetupCommand()._configure(interaction, REWARD_ROLE)

    assert "can't read your roles" in interaction.replied


async def test_a_direct_configure_call_still_refuses_an_overranked_role():
    interaction = FakeInteraction(user=invoker(SERVER_ADMIN, top_role=rank(1)))
    await SetupCommand()._configure(interaction, REWARD_ROLE)

    assert "not below your own highest role" in interaction.replied


async def test_configure_outside_a_guild_refuses_rather_than_raising():
    interaction = FakeInteraction(dm=True)
    await SetupCommand()._configure(interaction, REWARD_ROLE)

    assert "which server this is" in interaction.replied


# --------------------------------------------------------------------------- #
# The post-setup "Sync now" button
# --------------------------------------------------------------------------- #


async def test_only_the_admin_who_ran_setup_may_press_the_buttons():
    """The button starts the same full walk /sync gates on Manage Server.

    The reply is ephemeral, so in practice nobody else is shown it, but that
    is a keyword in another function; this check does not rely on it.
    """
    view = SyncPromptView("1", author_id=int(OWNER))
    try:
        passer = FakeInteraction(user=invoker(SERVER_ADMIN))
        assert await view.interaction_check(passer) is True
        assert passer.response.messages == []

        stranger = FakeInteraction(user=FakeMember(user_id(7), permissions=SERVER_ADMIN))
        assert await view.interaction_check(stranger) is False
        assert "Only the admin who ran /setup" in stranger.replied
        assert stranger.response.messages[0]["ephemeral"] is True
    finally:
        view.stop()


async def test_setup_binds_the_prompt_to_the_admin_who_ran_it(fresh_ui):
    interaction = await fresh_ui.run("setup", REWARD_ROLE)
    try:
        assert interaction.view.author_id == interaction.user.id
    finally:
        interaction.view.stop()
