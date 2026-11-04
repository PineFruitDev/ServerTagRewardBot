# ServerTagRewardBot

Reward members across the UGC space for representing your Discord community, from creators and game communities to studios and fan groups. This public bot grants a role the moment a member equips your server's tag and removes it when they stop, keeping tag adoption and role perks perfectly in sync. Invite it, run `/setup`, done.

[Visit Tag Rewards on Modulix](https://bots.modulix.gg/tag-rewards) for the hosted bot.

Built on the [PyTemplateBot](https://github.com/PineFruitDev/PyTemplateBot) architecture: command class pattern, single source of truth, discord.py.

**Website:** [bots.modulix.gg/tag-rewards](https://bots.modulix.gg/tag-rewards) · More Discord bots and free tools from Modulix: [bots.modulix.gg](https://bots.modulix.gg/) · [Discord account age checker](https://bots.modulix.gg/tools/account-age)

## Features

- **Public & Multi-Server**: One hosted bot serves unlimited servers; each configures its own reward
- **2-Minute Setup**: `/setup role:@Role` walks admins through configuration, including role-position checks
- **Live Tag Detection**: Reacts within seconds when members equip or remove your server tag
- **Automatic Role Sync**: Grants the reward role on equip, removes it on unequip, no manual work
- **First Sync, Your Call**: setup ends by asking "Would you like to sync your server tag rewards now?" with one-click buttons
- **Streaks & Leaderboard**: `/repped` shows anyone's current, longest, and all-time tag streaks; `/leaderboard` ranks the server's most loyal reppers
- **Admin Commands**: `/sync` for manual reconciliation, `/status` for live statistics, `/disable` to opt out
- **Spam Resistant**: Per-member and per-server command cooldowns with friendly "try again in 5 seconds" replies, and duplicate reward grants collapsed instead of re-applied
- **Production Ready**: Per-guild stats, rate-limit friendly syncing, and clean architecture

## Using the Bot (server admins)

1. Invite the bot with the Manage Roles permission
2. In Server Settings, Roles: drag the bot's role ABOVE the role you want to hand out
3. Run `/setup role:@YourRole` and answer the sync prompt at the end
4. That's it. Members repping your tag get the role within seconds; removing the tag removes the role. Check `/status` anytime.

Your server needs the Server Tag feature enabled for members to equip a tag.

## How It Works

Discord exposes a member's equipped server tag on the User object as `primary_guild` (`{ identity_guild_id, identity_enabled, tag, badge }`). A member is repping a server's tag when `identity_guild_id` matches that server and `identity_enabled` is true.

The bot watches raw `GUILD_MEMBER_UPDATE` gateway dispatches, which fire when that field changes, so role updates land in near real time across every server it's in. Per-server reward roles and streaks live in **tagbot-store**, a small Cloudflare Worker backed by D1 (encrypted at rest), reached over HTTPS with a bearer key, while the bot host still needs its runtime credentials; a full sync walks members and re-fetches users whose payloads omit the field, throttled to stay friendly with rate limits.

`GUILD_MEMBER_REMOVE` is watched too. Leaving a server ends a streak there: the time served is banked into the member's longest and all-time totals, and their current streak stops counting, because nobody left in the server is repping its tag on their behalf. A full sync can't do this for them, since it only walks the members who are still there.

### Cooldowns and rate limits

Commands and reward grants are both protected from being spammed, in
deliberately different ways, because only one of them is safe to refuse.

**Commands can be refused.** Every slash command carries two limits: one per
member per server (`COMMAND_COOLDOWN_SECONDS`, 5 by default) and one for the
server as a whole (`GUILD_COMMAND_LIMIT` uses per `GUILD_COMMAND_WINDOW_SECONDS`,
20 per minute by default). Whichever trips, the member gets a friendly ephemeral
reply telling them how long to wait, and the run costs no store round trip. The
two are weighed together before either is spent, so a member refused because the
server is busy does not also start their own cooldown. `/sync` sets its own
stricter limits (one per admin per minute, three per server per five minutes)
because it is the one command that costs an API call per member.

**Reward grants cannot be refused.** Dropping one would leave a member wearing a
role they no longer earn, or missing one they do, until something else noticed.
So grants are never rate limited; instead, repeats are collapsed. `GUILD_MEMBER_UPDATE`
fires for *every* member edit (a nickname, a timeout, someone else's roles), each
carrying the same unchanged tag state and costing a store write to re-record a
streak that has not moved. For `REWARD_COOLDOWN_SECONDS` after successful role
reconciliation and streak persistence, an event is skipped only if it both repeats that same decision and carries
roles that already agree with it. So nothing that has anything to say is ever
dropped: a tag that actually changed is acted on (equip, unequip and equip again
inside one second still produces all three role updates), an event showing the
reward role pulled by hand restores it, and a failed write records nothing at all
so the next event retries it. `/status` reports how many events were skipped.

Every limit is off at `0`, and all of them are per process and in memory, which
needs no storage and loses nothing worse than one redundant role write on a
restart.


Idempotent Discord role updates, member reads, and command registration retry
transient failures within fixed attempt and wait limits. Interaction replies
are not retried because their initial response token is single-use.

## Self-Hosting

### 1. Setup

Use Python 3.12 or 3.13. Node.js 22 is also required for Worker development
and tests. Run the commands below from the repository root. On Windows,
activate the virtual environment with `.venv\Scripts\Activate.ps1`.

```bash
git clone https://github.com/PineFruitDev/ServerTagRewardBot.git
cd ServerTagRewardBot
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

### 2. Configure

Configure the variable names listed below in your local `.env` or secret manager.
Keep credentials out of commits, logs, screenshots, and PR descriptions.
The test suites do not need a configured `.env` or live credentials.

> **Required Discord setup:** enable the **Server Members Intent** on the Bot page of the [Developer Portal](https://discord.com/developers/applications) and invite with the **Manage Roles** permission.
>
> **Limited mode:** if Discord refuses the Server Members intent (close code 4014, e.g. while a verified bot's intent request is pending), the bot logs a warning and reconnects without it. Slash commands keep working from the store; live tag sync and role updates stay off until the intent is granted and the bot restarts. Use `MEMBERS_INTENT` to control whether that intent is requested.
>
> **Storage:** deploy the Worker in `worker/` (see `worker/wrangler.toml`), then set `TAGBOT_STORE_URL` and `TAGBOT_STORE_KEY`. `scripts/migrate_sqlite_to_d1.py` imports a legacy `data.db`; `scripts/smoke_test.py` checks a running store.

### 3. Register and run

```bash
python register.py   # Register commands with Discord
python main.py       # Start the bot
```

Startup validates every setting before it opens a connection, and reports all
the problems it found at once rather than one per restart:

```
ServerTagRewardBot did not start: 2 configuration problem(s).
  - DISCORD_TOKEN is required but not set (the bot token from the Discord Developer Portal)
  - TAGBOT_STORE_URL is set but unusable: must be an https:// URL; plain http is only
    accepted for a local wrangler dev store on localhost or 127.0.0.1
```

No value is ever printed back, not even a masked fragment of one, so startup
output and CI logs stay safe to paste into an issue.

### Stopping the bot

Ctrl-C (`SIGINT`) and a supervisor's `SIGTERM` both shut down gracefully:
the bot stops accepting new background work, lets any sync already in flight
finish (up to 5 seconds), then closes the gateway connection and the HTTPS
session to the store. A sync interrupted halfway through a server's members
loses nothing that was already written, but the next start has to rediscover
where it got to, which is what the drain avoids. Work still running when the
grace period expires is cancelled and logged, so a wedged sync cannot hold the
process open past a supervisor's own kill timeout.

The grace period is deliberately shorter than the 10 seconds Docker allows
before `SIGKILL`, leaving room for the close itself. If you run with a tighter
`--stop-timeout`, lower `DRAIN_TIMEOUT` in `src/services/lifecycle.py` to
match, or the process can be killed partway through the clean close.

### When a command fails

Discord gives an interaction a few seconds to be answered, so anything a
command raises has to become a reply or the person who ran it is left with
"the application did not respond" and no idea whether it took effect.
`src/services/command_errors.py` is the one place that decides what a failure
means, and both interaction surfaces go through it: a slash command callback
via `CommandTree.on_error`, and a button on that command's own reply via
`GuardedView`. The library's default `View.on_error` only logs, so a button
left on a plain `discord.ui.View` answers nobody.

A failure produces one ephemeral message and one log line. The message names
the cause when the cause is something the reader can act on (a reward role
sitting above the bot's own, a rate limit, a storage outage, a stale command
list) and stays vague when it is a defect, since there is nothing useful to
say and plenty to leak. No exception message, repr, traceback, response body
or URL reaches either the reply or the log: `JsonFormatter` reduces exception
arguments to their type name, so an unexpected error is logged with its type
and the file and line that raised it instead.

### What the logs look like

Every line is one JSON object on stdout, so a host's log collector can index
it without a parser. Four keys are always present (`timestamp`, `level`,
`logger`, `message`) and a line handling an interaction carries five more:

```json
{"timestamp": "2026-11-01T12:00:00.123456+00:00", "level": "WARNING",
 "logger": "StoreClient", "message": "request - GET /guilds/900 failed (HTTP 503), retry 1/3 in 0.42s (backoff)",
 "correlation_id": "1a2c4e6f8b0d2", "command": "status", "guild": "900000000000000001", "user": "700000000000000001"}
{"timestamp": "2026-11-01T12:00:00.567890+00:00", "level": "INFO",
 "logger": "Commands", "message": "observed - /status finished",
 "correlation_id": "1a2c4e6f8b0d2", "command": "status", "guild": "900000000000000001", "user": "700000000000000001",
 "outcome": "ok", "latency_ms": 444}
```

`correlation_id` is derived from Discord's own interaction id, so every line a
single command produces shares one value: the command's, the store client's
retries, and the failure path's, which recomputes the id rather than relying
on state the command has already torn down. Background work a command starts
inherits it too, so a sync that `/setup` kicked off is findable from the
`/setup` that kicked it off.

Every invocation closes with exactly one `outcome` line (`ok`, `denied`,
`error` or `cancelled`), which makes counting failures a filter on one field
rather than a search through prose. Most carry `latency_ms` as well; the
exception is a failure that never reached the command at all, which is what an
unresolvable option or a stale registration produces. Those are still closed
out as `error`, because a failure count that quietly omitted exactly the
failures a bad deploy causes would be worse than useless, but they carry no
latency, since nothing started a clock and a zero would read as a command that
ran instantly.

`user` is who asked, not who was affected. A command's own lines carry the
invoker's id; the background sync a command spawns drops the field entirely at
the `src/services/tasks.py` boundary, because a full sync walks every member
of a guild and attributing thousands of "granted the role" lines to the one
admin who pressed the button would make a filter on that field answer the
wrong question.

Nothing else is allowed into a line. `src/services/logger.py` keeps an
allowlist of field names and drops everything else, because serializing
whatever a caller attached is how a request header or a response body ends up
in a log file. Field values are redacted exactly like messages are: the values
of credential-named environment variables are replaced wherever they appear,
as are URLs (which can carry signed query strings) and anything written as
`token=`, `Authorization: Bearer` and so on.

Two independent rules keep credentials out. `JsonFormatter` reduces an
exception argument to its type name, so a store error quoting the header it
was rejected for never reaches a message in the first place; redaction at the
output boundary then catches anything a call site formats in as a string, so a
careless `log.info` cannot leak one either. `tests/test_log_redaction.py`
drives every command, the failure path, the store client's retries, startup
validation and discord.py's own rate-limit warnings with both credentials set,
and fails if any ten consecutive characters of either survives into the
output. Ten rather than the whole value, so that a masked prefix fails too.

Ids in the fields are Discord snowflakes, which are public. Display names are
deliberately never logged: they identify a person far more than an id does,
and they are user-controlled text going into a system that reads logs.

## Project Structure

```
src/
├── core/
│   ├── bot.py                # Main bot class
│   ├── command.py            # Abstract command base
│   ├── options.py            # ← Bounds and shape checks for slash command options
│   ├── command_manager.py    # Command management
│   └── view.py               # Button/menu base that reports failures like a command
├── commands/
│   ├── __init__.py           # ← Command registry (single source of truth)
│   ├── setup.py              # /setup and /disable (admin onboarding)
│   ├── repped.py             # Streak lookup
│   ├── leaderboard.py        # Top reppers
│   ├── status.py             # Per-server sync statistics
│   ├── sync.py               # Manual full sync (admin)
│   ├── ping.py               # Latency check
│   └── help_command.py       # Auto-generated help
└── services/
    ├── tag_sync.py           # ← Tag detection + role sync engine (multi-guild)
    ├── cooldowns.py          # Command rate limits + reward duplicate collapsing
    ├── guild_config.py       # Per-server reward roles (tagbot-store)
    ├── streaks.py            # Streak tracking (tagbot-store)
    ├── logger.py             # Structured JSON logging with credential redaction
    ├── observability.py      # Per-command correlation id, outcome, and latency
    ├── command_errors.py     # ← One failure path for every command and button
    ├── discord_errors.py     # Classifies Discord API failures (fixable vs transient)
    ├── discord_retry.py      # Bounded retry/backoff for Discord calls, honouring retry_after
    ├── backoff.py            # Shared Retry-After parsing and jittered backoff
    ├── store_client.py       # HTTPS client for tagbot-store (retries, timeouts)
    ├── lifecycle.py          # Graceful shutdown on SIGINT/SIGTERM
    ├── tasks.py              # Background task supervision and shutdown drain
    └── environment.py        # Startup config validation
main.py                       # Entry point
register.py                   # Command registration
```

## Commands

- `/setup role:@Role` - Configure this server's reward role (defaults to Manage Server access)
- `/repped [user]` - Current, longest, and all-time tag streaks for you or another member
- `/leaderboard [sort]` - Top reppers by current streak, longest streak, or all-time repped
- `/status` - This server's sync statistics
- `/sync` - Manual full member sync (requires Manage Server)
- `/disable` - Stop managing the reward role in this server (requires Manage Server)
- `/ping` - Basic ping/pong with latency
- `/help [command]` - Auto-generated help system

### Who can run the admin commands

`/setup`, `/disable` and `/sync` change state, so each requires **Manage Server** or
**Administrator**, and the bot checks that itself on every run. Discord's
`default_member_permissions` only sets the default a server can reassign under Server
Settings, Integrations, Command Permissions, so it is treated as the convenience it is
rather than as the gate. Anyone else gets a private reply naming what they need.

Choosing the reward role is held to a higher bar than running the command, because the
bot hands that role out on your behalf and Manage Server grants no role control at all.
`/setup` mirrors Discord's own rule for assigning a role, so to pick one you need
**Manage Roles** and the role must sit **below your own highest role**:

- a role carrying **Administrator** is never accepted, since the reward goes to everyone
  who equips the tag. This one applies to the owner too
- **Administrator does not lift you over the hierarchy.** Discord allows only the server
  owner to assign a role above their own, so an Administrator ranked below a privileged
  role still cannot make it the reward
- the **server owner** is the one exception to the rank rule

Without this, Manage Server on its own would be enough to name a privileged role as the
reward, equip the tag and be handed it.

The bot's own role still has to sit above the reward role for it to be granted at all.
`/setup` checks that before it writes anything, and `/sync` reports it if it changes
later. The "Sync now" button `/setup` offers is bound to the admin who ran it
and rechecks their current permissions and the `/sync` cooldown when pressed.

### What each option accepts

Every option is bounded twice: once in the declaration Discord validates against,
and once at runtime in `src/core/options.py`, on the value that actually arrived.
The second pass is not redundant. Discord checks an option against the command
definition it holds on file, which is a cached copy of a past build, and
the bot refreshes that definition on its first ready event. A client can still
send a stale option while definitions propagate. discord.py closes only
part of that gap itself: an undeclared **choice** value raises before the callback
runs, but a **`Range` is declaration-only** (`RangeTransformer` passes the value
through untouched), so it bounds nothing once the command is live.

| Option | Declared | Checked again at runtime |
|--------|----------|--------------------------|
| `/setup role` | role, resolved in this guild | an id Discord could have issued, belonging to this server, before anything is persisted |
| `/repped user` | member, resolved in this guild | the same, before the streak lookup |
| `/leaderboard sort` | three choices | falls back to current streak, so the heading always names the ordering actually used |
| `/help command` | `Range[str, 1, 32]` | clamped and markdown-escaped before being quoted back, and sent with no allowed mentions |

Ids get this treatment because the store answers a malformed path segment with a
flat `400 invalid guild_id`, which the bot cannot turn into anything useful for
the person who ran the command. The gateway path has shape-checked every id since
the public rewrite; the command path now shares that one definition of the rule
instead of trusting `str(interaction.guild_id)`, which turns a missing guild into
the literal string `"None"`.

A refusal is always a private (ephemeral) reply naming the option at fault, and
nothing reaches the store or Discord's REST API before it. A walk of
`src/commands` keeps this from quietly regressing: it fails if an option is
declared without a range, a set of choices, or a resolved entity type, if an
option has no description, or if a name or description outgrows Discord's own
limits, catching that last one in CI rather than as a 400 from a hand-run
`register.py`.


## Environment Variables

Names only; supply credentials privately. The bot loads `.env` at startup.
The Worker also requires the `TAGBOT_STORE_KEY` secret and the `DB` D1 binding
configured in `worker/wrangler.toml`. Registration contacts Discord; run it
only when you intend to update the application’s slash commands.

| Variable | Required | Purpose |
|----------|----------|---------|
| `DISCORD_TOKEN` | Yes | Discord bot authentication |
| `TAGBOT_STORE_URL` | Yes | Storage Worker endpoint |
| `TAGBOT_STORE_KEY` | Yes | Shared bearer secret for bot and Worker |
| `DEVELOPER_IDS` | No | Developer-command allowlist |
| `ENVIRONMENT` | No | Application logging mode |
| `SYNC_ON_START` | No | Reconcile configured servers after startup |
| `SYNC_DELAY_MS` | No | Delay before each user re-fetch during full sync |
| `MEMBERS_INTENT` | No | Request the Server Members privileged intent |
| `COMMAND_COOLDOWN_SECONDS` | No | Wait between one member's uses of the same command in one server. `0` disables |
| `GUILD_COMMAND_LIMIT` | No | Uses of one command allowed per server per window. `0` disables |
| `GUILD_COMMAND_WINDOW_SECONDS` | No | Length of that per-server window. `0` disables |
| `REWARD_COOLDOWN_SECONDS` | No | How long an applied reward decision suppresses repeats of itself. `0` applies every repeat |

Variable names are listed without values. Validation rules and defaults are
implemented in [`environment.py`](src/services/environment.py).

### Permissions

`/setup`, `/disable`, and `/sync` declare Manage Server as their default
Discord command permission. Server administrators can override application
command permissions in Discord; the current shared runtime guard checks the
**bot's** Manage Roles permission, not the invoker's Manage Server permission.
Review command access in server integration settings accordingly.

The bot's highest role must be above the reward role. `/setup` rejects the
server's default role and managed integration roles. `/disable` stops managing
rewards but leaves existing member roles in place. Other commands declare no
administrative permission requirement.

For deployment prerequisites, failure recovery, and log interpretation, see the
[operator runbook](docs/operator-runbook.md).

## License Note
This project is licensed under **[Apache 2.0](https://choosealicense.com/licenses/apache-2.0/) + the [Commons Clause](https://commonsclause.com/)**. In plain terms:

- ✅ **Free to use and self-host.** Invite the hosted bot or run your own copy for any community, at no cost.
- ✅ **Forking and contributing is welcome.** Fork the repo, modify the code, and open a PR. Community contributions are encouraged.
- ❌ **You cannot sell the bot itself.** The Commons Clause means you may not sell a product or service whose value derives *primarily* from this bot (for example, reselling it as a paid hosted service or as a paid bot product).

In short: run the bot for whatever community you want, just do not resell the bot itself. See [LICENSE](LICENSE) for the full terms.

## Contributing

### Running tests

Use Python 3.12 or 3.13 and Node.js 22. Install the development dependencies,
then run both suites from the repository root:

```bash
python -m pip install -r requirements-dev.txt
python -m ruff check .
python -m mypy
python -m pytest -q --cov=src --cov=scripts --cov=main --cov=register --cov-branch --cov-report=term-missing --cov-fail-under=97
npm --prefix worker ci
node --check worker/src/index.js
npm --prefix worker test
```

The Python suite covers tag detection, role reconciliation, pagination and sync
failures, cooldowns and reward duplicate collapsing, slash command wiring and guards, the post-setup sync prompt, command
and button failure reporting (including what the reply and the log leave out),
per-command correlation and outcome reporting, credential redaction across
every surface that writes a log line, configuration, storage retries and
caching, streak math, both entry points (startup order, the process exit
codes, and command registration), and the one-time SQLite-to-D1 migration.
Discord calls are mocked; HTTP client tests use a loopback server. Pytest
blocks non-loopback connections, ignores `.env`, and isolates configuration
and service singletons between tests. No Discord or Cloudflare credentials are
needed, and the redaction tests use synthetic stand-ins shaped like the real
ones.

The log assertions swap the stream on the handlers the bot installed at import
time rather than adding a handler of their own, so what a test reads is what
would have reached stdout, through the formatter the bot really configured.

`mypy.ini` holds the typecheck scope. It checks every production module with no
per-module error suppressions, so every error code is enforced everywhere. Where
discord.py's annotations and this bot's runtime used to disagree, the code now
narrows or widens explicitly at the call site instead. Tests are not
typechecked.

The migration tests replay the script's generated SQL onto a database built
from `worker/migrations/0001_initial.sql`, so the generated statements and the
real schema cannot drift apart. The write path of `scripts/smoke_test.py` is
only exercised against a real `wrangler dev` store, as documented in that
script; faking it in Python would duplicate the Worker and prove nothing.

The Worker suite uses Node's test runner and [Miniflare's local D1 runtime](https://developers.cloudflare.com/d1/best-practices/local-development/#test-programmatically).
It applies the checked-in migration to temporary databases and tests the actual
HTTP routes and SQL for authorization, validation, guild configuration, streak
transitions, and guild isolation. It does not use `wrangler.toml`, remote D1, or
deployment commands. Dependencies must be installed before running offline.

`docs/coverage-gaps.md` records what the suites measure, what they do not, and
why, re-measured with the commands above. Read it before widening or loosening
the coverage scope.

GitHub Actions runs Python tests on 3.12 and 3.13 with a 97% branch-inclusive
coverage floor across `src`, `scripts`, `main.py` and `register.py`, a typecheck
job, and the Worker suite on Node.js 22, for pull requests and pushes to `main`.
Workflow permissions are read-only, actions are pinned to full commit SHAs,
checkout does not persist credentials, and superseded pull request runs are
cancelled. This verifies local behavior, not a live Discord connection or
production deployment.

### Submitting changes

1. Fork the repository
2. Create your feature branch
3. Commit your changes
4. Push to the branch
5. Create a Pull Request
