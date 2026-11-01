"""Rate limits: the windows themselves, the commands, and the reward path.

The rest of the suite runs with limits off (see config_for_tests) so that a
test is free to run one command twice. This file turns them on, against a clock
it controls, and checks the two halves separately:

* a command that is refused must say so, cost nothing, and come back on time;
* a reward grant that is collapsed must only ever be a repeat, because
  dropping a real change would leave a member with the wrong role.
"""

import pytest

from src.services.cooldowns import (Cooldowns, GUILD_SCOPE, Policy, RateLimiter, USER_SCOPE,
                                    Wait, wait_text)
from src.services.environment import Environment
from tests.integration_harness import (Clock, FakeInteraction, FakeMember, FakeRole, GUILD,
                                       ROLE, config_for_tests, field_values, user_id)

ALICE = user_id(1)
BOB = user_id(2)


@pytest.fixture
def ticking():
    """A monotonic clock that only moves when the test moves it."""
    return Clock(0)


def limits(clock, **policy) -> Cooldowns:
    """Install a Cooldowns singleton with the given policy (defaults where omitted)."""
    return Cooldowns(Policy(**policy), clock)


# --------------------------------------------------------------------------- #
# The windows
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("seconds,expected", [
    (0, "1 second"), (0.1, "1 second"), (1, "1 second"), (1.2, "2 seconds"),
    (4.0, "4 seconds"), (59, "59 seconds"), (60, "1 minute"), (61, "1 minute 1 second"),
    (125, "2 minutes 5 seconds"), (300, "5 minutes"),
])
def test_a_wait_is_rounded_up_and_never_reads_as_zero(seconds, expected):
    """"Try again in 0 seconds" reads like a bug to whoever was just refused."""
    assert wait_text(seconds) == expected


def test_a_cooldown_is_a_window_that_allows_one_use(ticking):
    limiter = RateLimiter(ticking)
    assert limiter.check("k", 1, 5) == 0.0
    assert limiter.check("k", 1, 5) == 5.0
    ticking.advance(4)
    assert limiter.check("k", 1, 5) == 1.0
    ticking.advance(1)
    assert limiter.check("k", 1, 5) == 0.0


def test_a_window_allows_its_whole_budget_then_frees_slots_one_at_a_time(ticking):
    limiter = RateLimiter(ticking)
    for _ in range(3):
        assert limiter.check("k", 3, 60) == 0.0
        ticking.advance(10)
    assert limiter.check("k", 3, 60) == 30.0  # the first use leaves the window at 60

    # It slides: one slot comes back at a time, rather than the budget
    # resetting in a block that a burst can line itself up against.
    ticking.advance(30)
    assert limiter.check("k", 3, 60) == 0.0
    assert limiter.check("k", 3, 60) == 10.0


def test_a_refused_use_is_not_recorded(ticking):
    """Hammering a command must not push its own cooldown further out."""
    limiter = RateLimiter(ticking)
    limiter.check("k", 1, 10)
    for _ in range(5):
        ticking.advance(1)
        limiter.check("k", 1, 10)
    ticking.advance(5)
    assert limiter.check("k", 1, 10) == 0.0


def test_a_peek_reports_the_wait_without_spending_the_budget(ticking):
    limiter = RateLimiter(ticking)
    assert limiter.check("k", 1, 5, record=False) == 0.0
    assert len(limiter) == 0
    limiter.check("k", 1, 5)
    assert limiter.check("k", 1, 5, record=False) == 5.0
    ticking.advance(5)
    assert limiter.check("k", 1, 5, record=False) == 0.0


def test_forgetting_a_key_starts_it_over(ticking):
    limiter = RateLimiter(ticking)
    limiter.check("k", 1, 30)
    limiter.forget("k")
    assert limiter.check("k", 1, 30) == 0.0
    limiter.forget("never-used")  # must not raise


@pytest.mark.parametrize("uses,per_seconds", [(0, 60), (1, 0), (0, 0), (-1, 60), (1, -1)])
def test_a_limit_of_zero_is_off(ticking, uses, per_seconds):
    """One operator who wants no cooldowns should not need a second switch."""
    limiter = RateLimiter(ticking)
    assert [limiter.check("k", uses, per_seconds) for _ in range(50)] == [0.0] * 50
    assert len(limiter) == 0


def test_expired_keys_are_swept_so_a_public_bot_does_not_grow_forever(ticking):
    limiter = RateLimiter(ticking, sweep_every=10)
    for n in range(9):
        limiter.check(f"gone-{n}", 1, 5)
    assert len(limiter) == 9
    ticking.advance(6)
    limiter.check("kept", 1, 60)  # the tenth check triggers the sweep
    assert len(limiter) == 1
    # Only the expired keys went: the live one still refuses a second use.
    assert limiter.check("kept", 1, 60) == 60.0


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def test_the_configured_policy_is_what_commands_are_held_to(monkeypatch):
    monkeypatch.setattr(Environment, "_config", config_for_tests(
        command_cooldown_seconds=7, guild_command_limit=3,
        guild_command_window_seconds=90, reward_cooldown_seconds=11,
    ))
    assert Policy.from_config() == Policy(7, 3, 90, 11)
    # get_instance builds from the environment when nothing was installed.
    assert Cooldowns.get_instance().policy == Policy(7, 3, 90, 11)


def test_a_member_is_asked_to_wait_and_told_how_long(ticking):
    cooldowns = limits(ticking, user_seconds=5)
    assert cooldowns.check_command("repped", ALICE, GUILD) is None
    waiting = cooldowns.check_command("repped", ALICE, GUILD)
    assert waiting is not None and waiting.scope == USER_SCOPE
    assert "`/repped`" in waiting.message("repped")
    assert "5 seconds" in waiting.message("repped")
    ticking.advance(5)
    assert cooldowns.check_command("repped", ALICE, GUILD) is None


def test_one_members_cooldown_is_their_own_and_only_in_that_server(ticking):
    cooldowns = limits(ticking, user_seconds=5, guild_uses=0)
    cooldowns.check_command("repped", ALICE, GUILD)
    assert cooldowns.check_command("repped", BOB, GUILD) is None
    assert cooldowns.check_command("repped", ALICE, "900000000000000009") is None
    assert cooldowns.check_command("leaderboard", ALICE, GUILD) is None
    assert cooldowns.check_command("repped", ALICE, GUILD) is not None


def test_a_server_budget_is_shared_and_says_so(ticking):
    cooldowns = limits(ticking, user_seconds=0, guild_uses=2, guild_seconds=60)
    assert cooldowns.check_command("leaderboard", ALICE, GUILD) is None
    assert cooldowns.check_command("leaderboard", BOB, GUILD) is None
    waiting = cooldowns.check_command("leaderboard", user_id(3), GUILD)
    assert waiting is not None and waiting.scope == GUILD_SCOPE
    assert "being used a lot in this server" in waiting.message("leaderboard")
    # Another server has its own budget.
    assert cooldowns.check_command("leaderboard", ALICE, "900000000000000009") is None


def test_a_run_refused_by_one_limit_does_not_spend_the_other(ticking):
    """Otherwise a busy server quietly puts every member on cooldown too."""
    cooldowns = limits(ticking, user_seconds=30, guild_uses=1, guild_seconds=60)
    cooldowns.check_command("status", ALICE, GUILD)  # spends the server's only use

    assert cooldowns.check_command("status", BOB, GUILD).scope == GUILD_SCOPE
    ticking.advance(60)
    # Bob was refused, so his own 30s cooldown never started.
    assert cooldowns.check_command("status", BOB, GUILD) is None


def test_a_dm_has_a_member_cooldown_and_no_server_limit(ticking):
    cooldowns = limits(ticking, user_seconds=5, guild_uses=1, guild_seconds=60)
    assert cooldowns.check_command("ping", ALICE, None) is None
    assert cooldowns.check_command("ping", ALICE, None).scope == USER_SCOPE
    # The server limit was never touched by the DM, in either direction.
    assert cooldowns.check_command("ping", BOB, None) is None
    assert cooldowns.check_command("ping", BOB, GUILD) is None


def test_a_command_can_ask_for_stricter_limits_than_the_default(ticking):
    cooldowns = limits(ticking, user_seconds=5, guild_uses=20, guild_seconds=60)
    assert cooldowns.check_command("sync", ALICE, GUILD, user_seconds=60,
                                   guild_rate=(1, 300)) is None
    ticking.advance(10)  # past the default cooldown, inside this command's own
    assert cooldowns.check_command("sync", ALICE, GUILD, user_seconds=60,
                                   guild_rate=(1, 300)).scope == USER_SCOPE
    assert cooldowns.check_command("sync", BOB, GUILD, user_seconds=60,
                                   guild_rate=(1, 300)).scope == GUILD_SCOPE


# --------------------------------------------------------------------------- #
# Reward grants
# --------------------------------------------------------------------------- #


def test_a_repeat_of_an_applied_decision_is_collapsed(ticking):
    cooldowns = limits(ticking, reward_seconds=30)
    assert cooldowns.collapse_duplicate(GUILD, ALICE, True) is False
    cooldowns.record_reward(GUILD, ALICE, True)
    assert cooldowns.collapse_duplicate(GUILD, ALICE, True) is True
    ticking.advance(30)
    assert cooldowns.collapse_duplicate(GUILD, ALICE, True) is False


def test_a_changed_decision_is_never_collapsed(ticking):
    """The whole point: dropping a change leaves a member with the wrong role."""
    cooldowns = limits(ticking, reward_seconds=300)
    cooldowns.record_reward(GUILD, ALICE, True)

    assert cooldowns.collapse_duplicate(GUILD, ALICE, False) is False
    cooldowns.record_reward(GUILD, ALICE, False)
    # And back again, immediately, still inside the window.
    assert cooldowns.collapse_duplicate(GUILD, ALICE, True) is False
    cooldowns.record_reward(GUILD, ALICE, True)
    assert cooldowns.collapse_duplicate(GUILD, ALICE, False) is False


def test_collapsing_is_per_member_per_server(ticking):
    cooldowns = limits(ticking, reward_seconds=30)
    cooldowns.record_reward(GUILD, ALICE, True)
    assert cooldowns.collapse_duplicate(GUILD, BOB, True) is False
    assert cooldowns.collapse_duplicate("900000000000000009", ALICE, True) is False


def test_collapsing_off_applies_every_repeat(ticking):
    cooldowns = limits(ticking, reward_seconds=0)
    cooldowns.record_reward(GUILD, ALICE, True)
    assert cooldowns.collapse_duplicate(GUILD, ALICE, True) is False
    assert cooldowns.tracked == 0


# --------------------------------------------------------------------------- #
# Through the real slash commands
# --------------------------------------------------------------------------- #


async def test_a_second_run_is_refused_without_reaching_the_store(ui, ticking):
    limits(ticking, user_seconds=5)

    first = await ui.run("repped", FakeMember(ALICE, name="Alice"))
    spent = len(ui.store.calls)
    second = await ui.run("repped", FakeMember(ALICE, name="Alice"))

    assert first.response.messages[0].get("embed") is not None
    assert "You can use `/repped` again in 5 seconds" in second.replied
    assert second.response.messages[0]["ephemeral"] is True
    # Refusing has to be cheaper than answering, or it is not worth doing.
    assert len(ui.store.calls) == spent


async def test_a_busy_server_refuses_a_member_who_has_run_nothing(ui, ticking):
    limits(ticking, user_seconds=0, guild_uses=2, guild_seconds=60)

    for member in (ALICE, BOB):
        await ui.run("leaderboard", interaction=FakeInteraction(user=FakeMember(member)))
    refused = await ui.run("leaderboard", interaction=FakeInteraction(user=FakeMember(user_id(3))))

    assert "being used a lot in this server" in refused.replied
    ticking.advance(60)
    allowed = await ui.run("leaderboard", interaction=FakeInteraction(user=FakeMember(user_id(3))))
    assert "Nobody has repped the tag yet" in allowed.replied


async def test_setup_and_disable_do_not_share_a_cooldown(ui, ticking):
    """An admin who disables rewards by mistake has to be able to undo it."""
    limits(ticking, user_seconds=300)

    await ui.run("disable")
    setup = await ui.run("setup", FakeRole())

    assert "Set up!" in setup.replied
    setup.view.stop()
    # The same command twice is still refused.
    assert "One moment" in (await ui.run("disable")).replied


async def test_a_full_sync_is_held_to_its_own_longer_cooldown(ui, ticking):
    """/sync is the most expensive thing the bot can be asked to do."""
    limits(ticking, user_seconds=5)

    await ui.run("sync")
    ticking.advance(30)  # well past the default cooldown
    refused = await ui.run("sync")

    assert "You can use `/sync` again in 30 seconds" in refused.replied
    ticking.advance(30)
    assert (await ui.run("sync")).followed_up.startswith("✅")


async def test_the_guard_reports_a_real_problem_rather_than_a_cooldown(fresh_ui, ticking):
    """A command refused for a reason the member can fix must say which."""
    limits(ticking, user_seconds=300)

    first = await fresh_ui.run("repped", interaction=FakeInteraction(dm=True))
    second = await fresh_ui.run("repped", interaction=FakeInteraction(dm=True))

    assert "only be used in servers" in first.replied
    # Validation runs first, so the refused run never started a cooldown.
    assert "only be used in servers" in second.replied


# --------------------------------------------------------------------------- #
# Through the real gateway path
# --------------------------------------------------------------------------- #


async def test_repeated_member_updates_cost_one_role_write_and_one_store_write(bot, ticking):
    """GUILD_MEMBER_UPDATE fires for every member edit, not just tag changes.

    The events after the first are what a nickname change looks like: same tag,
    and the reward role already on the member.
    """
    limits(ticking, reward_seconds=30)

    await bot.equip(ALICE)
    for _ in range(4):
        await bot.equip(ALICE, roles=[ROLE])

    assert bot.http.ops() == [("add_role", GUILD, ALICE, ROLE)]
    assert len(bot.store.mark_requests) == 1
    stats = bot.stats()
    assert (stats.live_updates, stats.duplicates_collapsed) == (5, 4)
    assert bot.has_reward(ALICE)


async def test_an_event_showing_the_role_gone_is_never_collapsed(bot, ticking):
    """An admin pulling the reward role by hand produces exactly this event."""
    limits(ticking, reward_seconds=300)

    await bot.equip(ALICE)
    await bot.equip(ALICE, roles=[])  # same tag, role no longer there

    assert bot.http.ops() == [("add_role", GUILD, ALICE, ROLE)] * 2
    assert bot.stats().duplicates_collapsed == 0
    assert bot.has_reward(ALICE)


async def test_every_real_tag_change_is_applied_however_fast_they_come(bot, ticking):
    limits(ticking, reward_seconds=300)

    await bot.equip(ALICE)
    await bot.unequip(ALICE, roles=[ROLE])
    await bot.equip(ALICE)

    assert bot.http.ops() == [
        ("add_role", GUILD, ALICE, ROLE),
        ("remove_role", GUILD, ALICE, ROLE),
        ("add_role", GUILD, ALICE, ROLE),
    ]
    assert bot.stats().duplicates_collapsed == 0
    assert bot.has_reward(ALICE)


async def test_a_failed_role_write_is_not_mistaken_for_settled_state(bot, ticking):
    """Only a successful write is recorded, so a failure is always retried."""
    cooldowns = limits(ticking, reward_seconds=300)
    bot.http.failures.add(("add_role", ALICE))

    await bot.equip(ALICE)
    assert bot.http.ops() == [("add_role", GUILD, ALICE, ROLE)]
    assert not bot.has_reward(ALICE)
    assert cooldowns.collapse_duplicate(GUILD, ALICE, True) is False

    bot.http.failures.clear()
    await bot.equip(ALICE)
    assert bot.has_reward(ALICE)
    assert bot.stats().duplicates_collapsed == 0
    # Now that it has stuck, the repeats of it are free.
    assert cooldowns.collapse_duplicate(GUILD, ALICE, True) is True


async def test_collapsing_stops_once_the_window_passes(bot, ticking):
    limits(ticking, reward_seconds=30)

    await bot.equip(ALICE)
    await bot.equip(ALICE, roles=[ROLE])
    ticking.advance(31)
    await bot.equip(ALICE, roles=[ROLE])

    # The role was already right, so the third event costs no API call either
    # way; what comes back is the streak write that proves it was not dropped.
    assert bot.stats().duplicates_collapsed == 1
    assert len(bot.store.mark_requests) == 2


async def test_a_join_without_the_tag_still_costs_nothing(bot, ticking):
    limits(ticking, reward_seconds=30)

    await bot.join(ALICE, tag_guild=None)

    assert bot.http.ops() == []
    assert (bot.stats().live_updates, bot.stats().duplicates_collapsed) == (0, 0)


async def test_status_reports_what_collapsing_saved(ui, ticking):
    limits(ticking, reward_seconds=30)

    await ui.equip(ALICE)
    await ui.equip(ALICE, roles=[ROLE])
    fields = field_values((await ui.run("status")).embed)

    assert fields["Live Updates Seen"] == "2"
    assert fields["Duplicate Updates Skipped"] == "1"
    assert fields["Roles Added"] == "1"


def test_a_wait_falls_back_to_the_server_wording_for_an_unknown_scope():
    """message() is reached from one place, but must never render a scope name."""
    assert "`/ping`" in Wait(1.0, "something-else").message("ping")
