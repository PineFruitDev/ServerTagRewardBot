"""Tag detection and reward granting, composed end to end.

test_tag_sync.py already covers TagSyncService on its own, with each collaborator
replaced by an AsyncMock. These tests instead run it against the real GuildConfig and
StreakStore, a store that enforces the Worker's contract, and a Discord stand-in that
remembers the roles it was told to set. So they assert on what a server actually ends
up with after a sequence of events (roles held, streak banked, counters shown) rather
than on which mock was called. See tests/integration_harness.py for the fakes.
"""

import asyncio

from src.services.store_client import StoreUnavailable
from tests.integration_harness import (GUILD, OTHER_GUILD, OTHER_ROLE, ROLE, TagBot,
                                       member_payload, primary_guild, user_id)

ALICE = user_id(1)
BOB = user_id(2)


# --------------------------------------------------------------------------- #
# Equipping and unequipping the tag
# --------------------------------------------------------------------------- #


async def test_equipping_the_tag_grants_the_role_and_starts_a_streak(bot, clock):
    await bot.equip(ALICE)

    assert bot.has_reward(ALICE)
    assert bot.http.ops() == [("add_role", GUILD, ALICE, ROLE)]
    assert await bot.streak(ALICE, now=clock.now) == {
        "current": 0, "longest": 0, "total": 0, "active": True,
    }


async def test_unequipping_the_tag_removes_the_role_and_banks_the_streak(bot, clock):
    await bot.equip(ALICE)
    clock.advance(3600)
    await bot.unequip(ALICE, roles=[ROLE])

    assert not bot.has_reward(ALICE)
    assert bot.http.ops() == [
        ("add_role", GUILD, ALICE, ROLE),
        ("remove_role", GUILD, ALICE, ROLE),
    ]
    assert await bot.streak(ALICE, now=clock.now) == {
        "current": 0, "longest": 3600, "total": 3600, "active": False,
    }


async def test_re_equipping_resumes_a_streak_rather_than_restarting_it(bot, clock):
    """Two equips in a row must not reset active_since and wipe the elapsed time."""
    await bot.equip(ALICE)
    clock.advance(600)
    await bot.equip(ALICE, roles=[ROLE])

    assert await bot.streak(ALICE, now=clock.now) == {
        "current": 600, "longest": 600, "total": 600, "active": True,
    }


async def test_identity_disabled_counts_as_not_repping(bot):
    """The tag can be set but switched off; that is not repping."""
    bot.http.grant(GUILD, ALICE, ROLE)
    await bot.dispatch("GUILD_MEMBER_UPDATE", {
        "guild_id": GUILD, **member_payload(ALICE, GUILD, enabled=False, roles=[ROLE]),
    })

    assert not bot.has_reward(ALICE)
    assert bot.http.ops() == [("remove_role", GUILD, ALICE, ROLE)]


async def test_live_update_counter_tracks_every_observed_event(bot):
    await bot.equip(ALICE)
    await bot.unequip(ALICE, roles=[ROLE])
    await bot.equip(BOB)

    stats = bot.stats()
    assert (stats.live_updates, stats.roles_added, stats.roles_removed) == (3, 2, 1)


# --------------------------------------------------------------------------- #
# Joining
# --------------------------------------------------------------------------- #


async def test_joining_with_the_tag_already_equipped_grants_the_role(bot):
    await bot.join(ALICE)

    assert bot.has_reward(ALICE)
    assert bot.http.ops() == [("add_role", GUILD, ALICE, ROLE)]


async def test_joining_without_the_tag_costs_no_api_calls(bot):
    """A join is not a transition, so a member with no tag needs no removal call."""
    await bot.join(ALICE, tag_guild=None)

    assert bot.http.ops() == []
    assert bot.store.mark_requests == []


# --------------------------------------------------------------------------- #
# Guild scoping
# --------------------------------------------------------------------------- #


async def test_unconfigured_guild_is_left_alone(fresh_bot):
    """No /setup means no role to grant, and no streak writes either."""
    await fresh_bot.equip(ALICE)
    await fresh_bot.join(BOB)

    assert fresh_bot.http.ops() == []
    assert fresh_bot.store.mark_requests == []
    assert fresh_bot.store.paths("GET") == [f"/guilds/{GUILD}", f"/guilds/{GUILD}"]


async def test_disabling_stops_role_management(bot):
    await bot.config.disable(GUILD)
    await bot.equip(ALICE)

    assert bot.http.ops() == []


async def test_each_guild_rewards_its_own_role_independently(bot):
    """One member, two servers: each one grants only its own configured role."""
    bot.store.seed_guild(OTHER_GUILD, OTHER_ROLE)

    await bot.equip(ALICE, guild_id=GUILD)
    await bot.dispatch("GUILD_MEMBER_UPDATE", {
        "guild_id": OTHER_GUILD, **member_payload(ALICE, GUILD),
    })

    assert bot.roles_of(ALICE, GUILD) == {ROLE}
    # Repping GUILD's tag earns nothing in OTHER_GUILD, and costs a removal there
    assert bot.roles_of(ALICE, OTHER_GUILD) == set()
    assert bot.stats(GUILD).roles_added == 1
    assert bot.stats(OTHER_GUILD).roles_added == 0


# --------------------------------------------------------------------------- #
# Failure containment
# --------------------------------------------------------------------------- #


async def test_a_corrupt_role_id_in_the_store_does_not_wedge_every_update(bot):
    """A bad stored role must be rejected per call, not raise out of the listener."""
    bot.store.guilds[GUILD] = {"role_id": "not-a-role", "enabled": True}

    await bot.equip(ALICE)

    assert bot.http.ops() == []
    assert bot.stats().roles_added == 0
    # The event was still observed and the streak still recorded
    assert bot.stats().live_updates == 1
    assert bot.store.row(GUILD, ALICE) is not None


async def test_a_forbidden_role_call_does_not_kill_the_listener(bot):
    """Losing the role hierarchy race must not stop later members being processed."""
    bot.http.failures.add(("add_role", ALICE))

    await bot.equip(ALICE)
    await bot.equip(BOB)

    assert not bot.has_reward(ALICE)
    assert bot.has_reward(BOB)
    assert bot.stats().roles_added == 1
    # The streak is still recorded: the member is repping whether or not the call landed
    assert bot.store.row(GUILD, ALICE) is not None


async def test_a_store_outage_still_lets_the_role_through(store, test_config):
    """While the guild config is cached, a dead store must not block role updates.

    The reward role is the user-visible half of the feature, so a failed streak
    write has to be logged and stepped over rather than abort the update.
    """
    store.seed_guild(GUILD, ROLE)
    bot = TagBot(store, cache_ttl=60)
    await bot.equip(ALICE)  # warms the config cache the live bot relies on
    store.outage = StoreUnavailable("offline")

    await bot.unequip(ALICE, roles=[ROLE])

    assert not bot.has_reward(ALICE)
    assert bot.http.ops()[-1] == ("remove_role", GUILD, ALICE, ROLE)


async def test_an_unexpected_error_is_swallowed_by_the_raw_listener(bot, monkeypatch):
    """on_socket_raw_receive is the outermost frame; nothing may escape it."""
    async def boom(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(bot.config, "get_role", boom)
    await bot.equip(ALICE)  # must not raise


# --------------------------------------------------------------------------- #
# Full sync
# --------------------------------------------------------------------------- #


async def test_full_sync_reconciles_the_whole_guild(bot, clock):
    """Grant to tag-wearers, revoke from everyone else, leave correct state untouched."""
    bot.http.seed_members(GUILD, [
        member_payload(user_id(1), GUILD),                        # repping, needs the role
        member_payload(user_id(2), GUILD, roles=[ROLE]),          # repping, already correct
        member_payload(user_id(3), None, roles=[ROLE]),           # not repping, needs removal
        member_payload(user_id(4), None),                         # not repping, already correct
        member_payload(user_id(5), OTHER_GUILD, roles=[ROLE]),    # other server's tag
    ])
    bot.http.grant(GUILD, user_id(2), ROLE)
    bot.http.grant(GUILD, user_id(3), ROLE)
    bot.http.grant(GUILD, user_id(5), ROLE)

    stats = await bot.service.full_sync(GUILD)

    assert bot.http.ops() == [
        ("add_role", GUILD, user_id(1), ROLE),
        ("remove_role", GUILD, user_id(3), ROLE),
        ("remove_role", GUILD, user_id(5), ROLE),
    ]
    assert (stats.members_checked, stats.currently_repping) == (5, 2)
    assert (stats.roles_added, stats.roles_removed) == (1, 2)
    assert stats.last_sync_at is not None

    # Streaks match the roles: the two reppers are running, nobody else is
    assert [await bot.streak(user_id(n), now=clock.now) for n in (1, 2)] == [
        {"current": 0, "longest": 0, "total": 0, "active": True}] * 2
    assert all(bot.store.row(GUILD, user_id(n)) is None for n in (3, 4, 5))


async def test_full_sync_refetches_members_whose_payload_omits_the_tag(bot):
    """Member list rows can omit primary_guild, so the user has to be fetched."""
    bot.http.seed_members(GUILD, [
        member_payload(user_id(1), omit_primary_guild=True),
        member_payload(user_id(2), omit_primary_guild=True),
    ])
    bot.http.users[user_id(1)] = {"id": user_id(1), "primary_guild": primary_guild(GUILD)}
    bot.http.users[user_id(2)] = {"id": user_id(2), "primary_guild": primary_guild(None)}

    stats = await bot.service.full_sync(GUILD)

    assert ("get_user", user_id(1)) in bot.http.calls
    assert ("get_user", user_id(2)) in bot.http.calls
    assert bot.roles_of(user_id(1)) == {ROLE}
    assert bot.roles_of(user_id(2)) == set()
    assert (stats.members_checked, stats.currently_repping) == (2, 1)


async def test_full_sync_paginates_until_the_guild_is_exhausted(bot):
    """A page of exactly 1000 means there may be more; the cursor has to advance."""
    population = [member_payload(user_id(n), GUILD) for n in range(1, 1051)]
    bot.http.seed_members(GUILD, population)

    stats = await bot.service.full_sync(GUILD)

    pages = [call for call in bot.http.calls if call[0] == "get_members"]
    assert [call[3] for call in pages] == [0, int(user_id(1000))]
    assert stats.members_checked == 1050
    assert stats.roles_added == 1050


async def test_full_sync_keeps_streak_writes_inside_the_stores_batch_limit(bot):
    """FakeStore rejects an oversized batch exactly as the Worker does."""
    bot.http.seed_members(GUILD, [member_payload(user_id(n), GUILD) for n in range(1, 121)])

    await bot.service.full_sync(GUILD)

    sizes = [len(body["marks"]) for body in bot.store.mark_requests]
    assert sizes == [50, 50, 20]
    assert sum(sizes) == 120


async def test_full_sync_is_a_no_op_in_an_unconfigured_guild(fresh_bot):
    fresh_bot.http.seed_members(GUILD, [member_payload(user_id(1), GUILD)])

    stats = await fresh_bot.service.full_sync(GUILD)

    assert fresh_bot.http.calls == []
    assert stats.members_checked == 0


async def test_overlapping_full_syncs_do_not_run_twice(bot):
    """The second caller has to back off rather than double every role call."""
    entered, release = asyncio.Event(), asyncio.Event()
    bot.http.seed_members(GUILD, [member_payload(user_id(1), GUILD)])
    real_get_members = bot.http.get_members

    async def gated(guild_id, *, limit, after):
        entered.set()
        await release.wait()
        return await real_get_members(guild_id, limit=limit, after=after)

    bot.http.get_members = gated
    first = asyncio.create_task(bot.service.full_sync(GUILD))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert bot.service.is_syncing(GUILD) is True
        second = await bot.service.full_sync(GUILD)
        assert second.members_checked == 0
    finally:
        release.set()
        await first

    assert bot.service.is_syncing(GUILD) is False
    assert bot.http.ops() == [("add_role", GUILD, user_id(1), ROLE)]


async def test_a_second_sync_of_the_same_guild_is_idempotent(bot, clock):
    """Running /sync twice must not re-add roles or restart streaks."""
    bot.http.seed_members(GUILD, [member_payload(user_id(1), GUILD)])

    await bot.service.full_sync(GUILD)
    clock.advance(1800)
    # The member list now reflects the role the first sync granted
    bot.http.seed_members(GUILD, [member_payload(user_id(1), GUILD, roles=[ROLE])])
    stats = await bot.service.full_sync(GUILD)

    assert bot.http.ops() == [("add_role", GUILD, user_id(1), ROLE)]
    assert stats.roles_added == 1
    assert await bot.streak(user_id(1), now=clock.now) == {
        "current": 1800, "longest": 1800, "total": 1800, "active": True,
    }


# --------------------------------------------------------------------------- #
# Startup reconciliation across every configured guild
# --------------------------------------------------------------------------- #


async def test_sync_all_guilds_covers_every_configured_guild(bot):
    bot.store.seed_guild(OTHER_GUILD, OTHER_ROLE)
    bot.http.seed_members(GUILD, [member_payload(user_id(1), GUILD)])
    bot.http.seed_members(OTHER_GUILD, [member_payload(user_id(2), OTHER_GUILD)])

    await bot.service.sync_all_guilds()

    assert bot.roles_of(user_id(1), GUILD) == {ROLE}
    assert bot.roles_of(user_id(2), OTHER_GUILD) == {OTHER_ROLE}

