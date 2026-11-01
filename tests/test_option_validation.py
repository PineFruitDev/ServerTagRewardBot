"""Bounds on everything a slash command accepts.

Three layers are covered, because a bound that exists in only one of them is
not a bound:

* the helpers in src/core/options.py, directly;
* each command's wiring, by invoking the slash callback it registers and
  asserting the store was never reached when a value was refused;
* the declarations themselves, by walking src/commands with ast, so an option
  added later cannot skip the layer or outgrow Discord's own limits.
"""

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from src.commands import ALL_COMMANDS
from src.commands.help_command import HelpCommand, sanitize_name
from src.commands.leaderboard import LeaderboardCommand
from src.commands.repped import ReppedCommand
from src.commands.setup import SetupCommand
from src.commands.status import StatusCommand
from src.commands.sync import SyncCommand
from src.core.options import (GUILD_SCOPE_REFUSAL, MAX_SNOWFLAKE, bounded_choice, guild_scope,
                              option_id, require_guild_scope, safe_echo, snowflake,
                              target_refusal)
from src.services.guild_config import GuildConfig
from src.services.streaks import StreakStore
from src.services.tag_sync import TagSyncService

COMMANDS_DIR = Path(__file__).resolve().parent.parent / "src" / "commands"

# Discord's documented application-command limits.
MAX_NAME = 32
MAX_DESCRIPTION = 100
MAX_OPTIONS = 25
# Embed field values cap here, and /help builds one per category.
MAX_EMBED_FIELD = 1024

# An option type is bounded either because it declares a range or a closed set
# of choices, or because Discord resolves it to an entity in the invoking guild
# and there is no free-form length to bound. A bare str/int/float is neither.
BOUNDED_OPTION_TYPES = ("Range", "Choice", "Role", "Member", "User", "Attachment",
                        "TextChannel", "VoiceChannel", "Thread", "Permissions")


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

class Role:
    """Enough of discord.Role for the option checks, including rank comparison."""

    def __init__(self, *, id=10, guild_id=1, default=False, managed=False, position=1):
        self.id, self.mention = id, f"<@&{id}>"
        self.guild = None if guild_id is None else SimpleNamespace(id=guild_id)
        self.default, self.managed, self.position = default, managed, position
        # Not read by the option checks. Present so these fakes stay usable as
        # the admin commands grow checks of their own about what a role carries.
        self.permissions = SimpleNamespace(administrator=False)

    def is_default(self):
        return self.default

    def __ge__(self, position):
        # guild.me.top_role is an int here, so the reflected operand is used.
        return self.position >= position


class Member:
    """Enough of discord.Member for /repped, and to pass an admin command's guard."""

    def __init__(self, *, id=42, guild_id=1, bot=False):
        self.id, self.bot, self.display_name = id, bot, "Tester"
        self.guild = None if guild_id is None else SimpleNamespace(id=guild_id)
        self.display_avatar = SimpleNamespace(url="https://example.invalid/a.png")
        # The option checks ask nothing about the invoker's permissions, but
        # /setup, /disable and /sync do, so a fake that cannot clear their
        # guard would make these tests pass for the wrong reason.
        self.guild_permissions = SimpleNamespace(
            manage_guild=True, manage_roles=True, administrator=True,
        )
        self.top_role = 99


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


@pytest.fixture
def interaction():
    return SimpleNamespace(
        guild_id=1,
        guild=SimpleNamespace(id=1, owner_id=42, me=SimpleNamespace(
            top_role=10, guild_permissions=SimpleNamespace(manage_roles=True),
        )),
        user=Member(),
        command=SimpleNamespace(name="setup"),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock(),
                                 edit_message=AsyncMock(), is_done=lambda: False),
        followup=SimpleNamespace(send=AsyncMock()),
    )


@pytest.fixture
def store(monkeypatch):
    """Stand-ins for both per-guild stores, so a refusal's silence is visible."""
    config = SimpleNamespace(get_role=AsyncMock(return_value="10"),
                             set_role=AsyncMock(), disable=AsyncMock(return_value=True))
    streaks = SimpleNamespace(
        get=AsyncMock(return_value={"current": 0, "longest": 0, "total": 0, "active": False}),
        leaderboard=AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(GuildConfig, "_instance", config)
    monkeypatch.setattr(StreakStore, "_instance", streaks)
    monkeypatch.setattr(TagSyncService, "_instance", None)
    return SimpleNamespace(config=config, streaks=streaks)


def reply(interaction):
    """The single positional argument of the one reply that was sent."""
    interaction.response.send_message.assert_awaited_once()
    return interaction.response.send_message.await_args.args[0]


# --------------------------------------------------------------------------- #
# option_id: shape plus range
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("value,expected", [
    ("42", "42"), (42, "42"), (str(MAX_SNOWFLAKE), str(MAX_SNOWFLAKE)),
    # Shape-valid but not an id Discord can have issued.
    ("0", None), (0, None), (str(MAX_SNOWFLAKE + 1), None),
    # Rejected by the shape check it builds on.
    ("", None), ("12a", None), ("-1", None), (-1, None), ("1" * 21, None),
    (None, None), (True, None), (False, None), (1.5, None), (["1"], None),
    ("4 2", None), (" 42", None), ("0x2a", None),
])
def test_option_id_bounds_both_shape_and_range(value, expected):
    assert option_id(value) == expected


def test_option_id_is_stricter_than_the_gateway_shape_check():
    """The two rules differ on purpose, so neither can be swapped for the other.

    snowflake() guards gateway payloads, where the job is rejecting what cannot
    be parsed. option_id() guards command options, which get written to the
    store and spliced into REST paths, so it also asks whether Discord could
    have minted the value.
    """
    assert snowflake("0") == "0" and option_id("0") is None
    overflow = str(MAX_SNOWFLAKE + 1)
    assert snowflake(overflow) == overflow and option_id(overflow) is None


def test_snowflake_has_one_definition():
    """tag_sync re-exports it rather than keeping a second copy of the regex."""
    from src.core import options
    from src.services import tag_sync

    assert tag_sync.snowflake is options.snowflake
    assert "re.compile" not in Path(tag_sync.__file__).read_text()


# --------------------------------------------------------------------------- #
# guild scope
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("guild_id,expected", [
    (1, "1"), ("1", "1"), (None, None), (0, None), ("None", None), ("abc", None),
])
def test_guild_scope_validates_the_id_it_returns(interaction, guild_id, expected):
    interaction.guild_id = guild_id
    assert guild_scope(interaction) == expected


async def test_require_guild_scope_answers_the_user_when_there_is_no_guild(interaction):
    interaction.guild_id = None
    assert await require_guild_scope(interaction) is None
    assert GUILD_SCOPE_REFUSAL in reply(interaction)
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True


async def test_require_guild_scope_stays_quiet_when_the_guild_is_usable(interaction):
    assert await require_guild_scope(interaction) == "1"
    interaction.response.send_message.assert_not_awaited()


def test_a_missing_guild_id_never_becomes_the_string_none(interaction):
    """str(interaction.guild_id) used to post "None" to the store as a path segment."""
    interaction.guild_id = None
    assert str(interaction.guild_id) == "None"
    assert guild_scope(interaction) is None


# --------------------------------------------------------------------------- #
# target_refusal
# --------------------------------------------------------------------------- #

def test_a_target_from_this_guild_is_accepted(interaction):
    assert target_refusal(interaction, Role(), "role") is None
    assert target_refusal(interaction, Member(), "member") is None


def test_a_target_from_another_guild_is_refused(interaction):
    refusal = target_refusal(interaction, Role(guild_id=999), "role")
    assert refusal is not None and "isn't from this server" in refusal


@pytest.mark.parametrize("bad", [0, -1, "abc", None, MAX_SNOWFLAKE + 1])
def test_a_target_whose_id_is_unusable_is_refused(interaction, bad):
    refusal = target_refusal(interaction, Role(id=bad), "role")
    assert refusal is not None and "couldn't read that role's id" in refusal


def test_the_label_names_the_option_that_was_refused(interaction):
    assert "member's id" in target_refusal(interaction, Member(id=0), "member")


def test_a_target_that_states_no_guild_is_accepted_but_still_id_checked(interaction):
    """Not a shape discord.py produces, so it is not guessed at either way."""
    assert target_refusal(interaction, Role(guild_id=None), "role") is None
    assert target_refusal(interaction, Role(guild_id=None, id=0), "role") is not None


def test_a_target_is_refused_outright_when_the_guild_is_unknown(interaction):
    interaction.guild_id = None
    assert target_refusal(interaction, Role(), "role") == GUILD_SCOPE_REFUSAL


# --------------------------------------------------------------------------- #
# bounded_choice and safe_echo
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("value,expected", [
    ("current", "current"), ("longest", "longest"),
    ("retired", "total"), ("", "total"), (None, "total"), (7, "total"), (True, "total"),
])
def test_bounded_choice_falls_back_to_the_default(value, expected):
    assert bounded_choice(value, ("current", "longest", "total"), "total") == expected


@pytest.mark.parametrize("raw,expected", [
    ("ping", "ping"), ("  PING  ", "PING"), ("a b   c", "a b c"),
    ("**bold**", r"\*\*bold\*\*"), ("", "(blank)"), ("   ", "(blank)"),
])
def test_safe_echo_collapses_and_escapes(raw, expected):
    assert safe_echo(raw) == expected


def test_safe_echo_clamps_input_the_declared_range_did_not_stop():
    clamped = safe_echo("z" * 4000)
    assert clamped.endswith("…") and len(clamped) == 61


def test_safe_echo_honours_a_caller_supplied_limit():
    assert safe_echo("abcdef", limit=3) == "abc…"


def test_sanitize_name_still_delegates_to_the_shared_helper():
    assert sanitize_name("  **x**  ") == safe_echo("  **x**  ")


# --------------------------------------------------------------------------- #
# Wiring: /setup role
# --------------------------------------------------------------------------- #

async def test_setup_refuses_a_role_from_another_guild_without_writing(interaction, store):
    await register(SetupCommand())["setup"](interaction, Role(guild_id=999))
    assert "isn't from this server" in reply(interaction)
    store.config.set_role.assert_not_awaited()


async def test_setup_refuses_a_role_with_an_unusable_id_without_writing(interaction, store):
    await register(SetupCommand())["setup"](interaction, Role(id=0))
    assert "couldn't read that role's id" in reply(interaction)
    store.config.set_role.assert_not_awaited()


async def test_setup_refuses_an_unusable_guild_id_without_writing(interaction, store):
    interaction.guild_id = 0
    await register(SetupCommand())["setup"](interaction, Role())
    assert GUILD_SCOPE_REFUSAL in reply(interaction)
    store.config.set_role.assert_not_awaited()


async def test_setup_stores_the_validated_guild_scope(interaction, store):
    interaction.guild_id = 1
    await register(SetupCommand())["setup"](interaction, Role())
    store.config.set_role.assert_awaited_once_with("1", "10")


async def test_disable_refuses_an_unusable_guild_id_and_leaves_the_store_alone(interaction, store):
    """guild_only rejects a DM, but it says nothing about the id's shape, so the
    guild object is left in place here and only guild_id is made unusable."""
    interaction.guild_id = 0
    await register(SetupCommand())["disable"](interaction)
    assert GUILD_SCOPE_REFUSAL in reply(interaction)
    store.config.disable.assert_not_awaited()
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True


async def test_disable_passes_the_validated_scope_through(interaction, store):
    await register(SetupCommand())["disable"](interaction)
    store.config.disable.assert_awaited_once_with("1")


# --------------------------------------------------------------------------- #
# Wiring: /repped user
# --------------------------------------------------------------------------- #

async def test_repped_refuses_a_member_from_another_guild_without_a_lookup(interaction, store):
    await register(ReppedCommand())["repped"](interaction, Member(guild_id=999))
    assert "isn't from this server" in reply(interaction)
    store.streaks.get.assert_not_awaited()


async def test_repped_refuses_a_member_with_an_unusable_id(interaction, store):
    await register(ReppedCommand())["repped"](interaction, Member(id=0))
    assert "couldn't read that member's id" in reply(interaction)
    store.streaks.get.assert_not_awaited()


async def test_repped_defaults_to_the_invoker_and_still_validates(interaction, store):
    await register(ReppedCommand())["repped"](interaction, None)
    store.streaks.get.assert_awaited_once_with("1", "42")


# --------------------------------------------------------------------------- #
# Wiring: /leaderboard sort
# --------------------------------------------------------------------------- #

async def test_leaderboard_falls_back_when_a_stale_choice_arrives(interaction, store):
    """A Range is not enforced at runtime and a registration can go stale, so
    the heading has to describe the ordering actually used."""
    store.streaks.leaderboard = AsyncMock(return_value=[("7", 120)])
    await LeaderboardCommand()._show(interaction, "retired")
    assert store.streaks.leaderboard.await_args.kwargs["sort"] == "current"
    embed = interaction.response.send_message.await_args.kwargs["embed"]
    assert "Current Streak" in embed.title


async def test_leaderboard_keeps_a_declared_choice(interaction, store):
    store.streaks.leaderboard = AsyncMock(return_value=[("7", 120)])
    await LeaderboardCommand()._show(interaction, "longest")
    assert store.streaks.leaderboard.await_args.kwargs["sort"] == "longest"


# --------------------------------------------------------------------------- #
# Wiring: the guild scope every per-guild command depends on
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("command", [StatusCommand(), SyncCommand(), LeaderboardCommand(),
                                     ReppedCommand()])
async def test_a_per_guild_command_refuses_an_unusable_guild_id(interaction, store, command):
    interaction.guild_id = None
    await command.execute(interaction)
    assert GUILD_SCOPE_REFUSAL in reply(interaction)
    store.config.get_role.assert_not_awaited()
    store.streaks.get.assert_not_awaited()
    store.streaks.leaderboard.assert_not_awaited()


def test_no_command_builds_a_guild_id_by_stringifying_the_interaction():
    """The construction this change removed; it turned None into "None"."""
    offenders = [path.name for path in COMMANDS_DIR.glob("*.py")
                 if "str(interaction.guild_id)" in path.read_text()]
    assert offenders == []


# --------------------------------------------------------------------------- #
# Wiring: /help command
# --------------------------------------------------------------------------- #

async def test_help_bounds_an_oversized_name_the_range_let_through(interaction):
    await register(HelpCommand(list(ALL_COMMANDS)))["help"](interaction, "q" * 4000)
    body = reply(interaction)
    assert len(body) < 2000
    assert interaction.response.send_message.await_args.kwargs["allowed_mentions"].everyone is False


async def test_help_cannot_be_used_to_ping(interaction):
    await register(HelpCommand(list(ALL_COMMANDS)))["help"](interaction, "@everyone")
    mentions = interaction.response.send_message.await_args.kwargs["allowed_mentions"]
    assert (mentions.everyone, mentions.users, mentions.roles) == (False, False, False)


# --------------------------------------------------------------------------- #
# Declarations: a walk of src/commands, so a new option cannot skip the layer
# --------------------------------------------------------------------------- #

def _slash_name(node):
    """The name in @tree.command(name=...) on this function, or None."""
    for decorator in node.decorator_list:
        if (isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "command"):
            for keyword in decorator.keywords:
                if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                    return keyword.value.value
    return None


def _describe(node):
    """The option descriptions in @app_commands.describe(...) on this function."""
    for decorator in node.decorator_list:
        if (isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "describe"):
            return {kw.arg: kw.value.value for kw in decorator.keywords
                    if isinstance(kw.value, ast.Constant)}
    return {}


def _slash_commands():
    """(file, name, description, options) for every slash command declared."""
    found = []
    for path in sorted(COMMANDS_DIR.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            name = _slash_name(node)
            if name is None:
                continue
            described = _describe(node)
            options = [
                (arg.arg,
                 ast.unparse(arg.annotation) if arg.annotation is not None else "",
                 described.get(arg.arg, ""))
                for arg in node.args.args if arg.arg not in ("self", "interaction")
            ]
            found.append((path.name, name, node, options))
    return found


def test_the_declaration_walk_finds_every_command_and_option():
    """Non-vacuity: the assertions below are worthless if the walk finds nothing."""
    declared = _slash_commands()
    names = {name for _, name, _, _ in declared}
    assert names == {"setup", "disable", "sync", "status", "repped", "leaderboard",
                     "ping", "help"}
    options = {(name, option) for _, name, _, opts in declared for option, _, _ in opts}
    assert options == {("setup", "role"), ("repped", "user"),
                       ("leaderboard", "sort"), ("help", "command")}


def test_every_option_declares_a_bound():
    """A free-form str or an unbounded int has to say what it accepts.

    Discord enforces a declared bound against the definition it holds on file,
    which is the only thing standing between a user and the callback for a
    Range (discord.py's RangeTransformer passes the value through untouched).
    An option that declares nothing has no first line at all.
    """
    unbounded = [
        (file, command, option, annotation)
        for file, command, _, options in _slash_commands()
        for option, annotation, _ in options
        if not any(kind in annotation for kind in BOUNDED_OPTION_TYPES)
    ]
    assert unbounded == []


def test_every_option_is_described():
    """An undescribed option registers with an empty description, which Discord rejects."""
    missing = [(command, option) for _, command, _, options in _slash_commands()
               for option, _, description in options if not description]
    assert missing == []


@pytest.mark.parametrize("file,name,node,options", _slash_commands(),
                         ids=[f"{name}" for _, name, _, _ in _slash_commands()])
def test_a_declaration_fits_discords_limits(file, name, node, options):
    """Caught here rather than as a 400 from a hand-run register.py."""
    assert 1 <= len(name) <= MAX_NAME, f"{name} is not a valid command name length"
    assert name == name.lower(), f"{name} must be lowercase"

    description = next((kw.value.value for decorator in node.decorator_list
                        if isinstance(decorator, ast.Call)
                        and isinstance(decorator.func, ast.Attribute)
                        and decorator.func.attr == "command"
                        for kw in decorator.keywords
                        if kw.arg == "description" and isinstance(kw.value, ast.Constant)), "")
    assert 1 <= len(description) <= MAX_DESCRIPTION, f"/{name} description is {len(description)}"

    assert len(options) <= MAX_OPTIONS
    for option, _, option_description in options:
        assert 1 <= len(option) <= MAX_NAME
        assert len(option_description) <= MAX_DESCRIPTION


# --------------------------------------------------------------------------- #
# Declarations: the help embed built from them
# --------------------------------------------------------------------------- #

async def test_the_help_listing_fits_an_embed_field(interaction):
    """Every command's description lands in one shared field per category."""
    await register(HelpCommand(list(ALL_COMMANDS)))["help"](interaction, None)
    embed = interaction.response.send_message.await_args.kwargs["embed"]
    for field in embed.fields:
        assert len(field.value) <= MAX_EMBED_FIELD, f"{field.name} is {len(field.value)}"
    assert len(embed) <= 6000


def test_every_registered_command_has_help_text_for_its_usage():
    for command in ALL_COMMANDS:
        info = command.help_info
        assert info.usage.startswith(f"/{info.name}")
        assert info.description and len(info.description) <= MAX_DESCRIPTION


def test_discord_is_the_only_source_of_markdown_escaping():
    """safe_echo leans on discord.utils, so the behaviour tracks the library."""
    assert safe_echo("*x*") == discord.utils.escape_markdown("*x*")
