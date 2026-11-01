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

The bot watches raw `GUILD_MEMBER_UPDATE` gateway dispatches, which fire when that field changes, so role updates land in near real time across every server it's in. Per-server reward roles and streaks live in **tagbot-store**, a small Cloudflare Worker backed by D1 (encrypted at rest), reached over HTTPS with a bearer key, so nothing sensitive is stored on the bot host; a full sync walks members and re-fetches users whose payloads omit the field, throttled to stay friendly with rate limits.

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
streak that has not moved. For `REWARD_COOLDOWN_SECONDS` after a successful role
write, an event is skipped only if it both repeats that same decision and carries
roles that already agree with it. So nothing that has anything to say is ever
dropped: a tag that actually changed is acted on (equip, unequip and equip again
inside one second still produces all three role updates), an event showing the
reward role pulled by hand restores it, and a failed write records nothing at all
so the next event retries it. `/status` reports how many events were skipped.

Every limit is off at `0`, and all of them are per process and in memory, which
needs no storage and loses nothing worse than one redundant role write on a
restart.

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
> **Limited mode:** if Discord refuses the Server Members intent (close code 4014, e.g. while a verified bot's intent request is pending), the bot logs a warning and reconnects without it. Slash commands keep working from the store; live tag sync and role updates stay off until the intent is granted and the bot restarts. Set `MEMBERS_INTENT=false` to start in limited mode directly.
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

## Project Structure

```
src/
├── core/
│   ├── bot.py                # Main bot class
│   ├── command.py            # Abstract command base
│   └── command_manager.py    # Command management
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
    ├── discord_errors.py     # Classifies Discord API failures (fixable vs transient)
    ├── lifecycle.py          # Graceful shutdown on SIGINT/SIGTERM
    ├── tasks.py              # Background task supervision and shutdown drain
    └── environment.py        # Startup config validation
main.py                       # Entry point
register.py                   # Command registration
```

## Commands

- `/setup role:@Role` - Configure this server's reward role (requires Manage Server)
- `/repped [user]` - Current, longest, and all-time tag streaks for you or another member
- `/leaderboard [sort]` - Top reppers by current streak, longest streak, or all-time repped
- `/status` - This server's sync statistics
- `/sync` - Manual full member sync (requires Manage Server)
- `/disable` - Stop managing the reward role in this server
- `/ping` - Basic ping/pong with latency
- `/help [command]` - Auto-generated help system

## Environment Variables

Names only; supply credentials privately. The bot loads `.env` at startup.
The Worker also requires the `TAGBOT_STORE_KEY` secret and the `DB` D1 binding
configured in `worker/wrangler.toml`. Registration contacts Discord; run it
only when you intend to update the application’s slash commands.

| Variable | Required | Accepted values | Description |
|----------|----------|-----------------|-------------|
| `DISCORD_TOKEN` | ✅ | dotted string, 50+ characters | Bot token from Discord Developer Portal |
| `DEVELOPER_IDS` | ❌ | comma-separated 17-20 digit IDs | User IDs for developer commands |
| `ENVIRONMENT` | ❌ | `development` or `production` (default) | Environment mode; sets the log level. Anything else is logged and read as `production` |
| `SYNC_ON_START` | ❌ | boolean (default `false`) | Sync all configured servers at startup; /setup offers the first sync |
| `SYNC_DELAY_MS` | ❌ | whole milliseconds (default `350`) | Throttle before each user re-fetch during sync. Over `1000` is logged as a likely typo, since it applies per member, but is honoured |
| `TAGBOT_STORE_URL` | ✅ | `https://` URL, or `http://localhost` for `wrangler dev` | HTTPS URL of the tagbot-store Worker |
| `TAGBOT_STORE_KEY` | ✅ | 32+ characters | Bearer secret for the Worker (keep in a secrets manager) |
| `MEMBERS_INTENT` | ❌ | boolean (default `true`) | Request the Server Members intent; falls back to limited mode if refused |
| `COMMAND_COOLDOWN_SECONDS` | ❌ | whole seconds (default `5`) | Wait between one member's uses of the same command in one server. `0` disables |
| `GUILD_COMMAND_LIMIT` | ❌ | whole number (default `20`) | Uses of one command allowed per server per window. `0` disables |
| `GUILD_COMMAND_WINDOW_SECONDS` | ❌ | whole seconds (default `60`) | Length of that per-server window. `0` disables |
| `REWARD_COOLDOWN_SECONDS` | ❌ | whole seconds (default `30`) | How long an applied reward decision suppresses repeats of itself. `0` applies every repeat |

Booleans accept `true`/`1`/`yes`/`on` and `false`/`0`/`no`/`off`, in any case.
A blank value is treated as unset and takes the default; anything unparseable
fails startup rather than being quietly read as `false`.

> **If you are upgrading:** `SYNC_ON_START` previously recognised only the
> literal `true`, so `SYNC_ON_START=yes`, `on` or `1` meant *off*. Those forms
> now mean *on*, as documented, and the next restart will sync every configured
> server. Set it to `false` first if you do not want that.

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
python -m pytest -q --cov=src --cov=scripts --cov-branch --cov-report=term-missing --cov-fail-under=93
npm --prefix worker ci
node --check worker/src/index.js
npm --prefix worker test
```

The Python suite covers tag detection, role reconciliation, pagination and sync
failures, slash command wiring and guards, cooldowns and reward duplicate
collapsing, the post-setup sync prompt, configuration, storage retries and
caching, streak math, and the one-time SQLite-to-D1 migration. Discord calls are mocked; HTTP client tests use a
loopback server. Pytest blocks non-loopback connections, ignores `.env`, and
isolates configuration and service singletons between tests. No Discord or
Cloudflare credentials are needed.

`mypy.ini` holds the typecheck scope. It checks every production module, and
baselines a single error code per module where discord.py's own annotations
disagree with its runtime payloads, so every other error stays enforced
everywhere. Tests are not typechecked.

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

GitHub Actions runs Python tests on 3.12 and 3.13 with a 93% branch-inclusive
coverage floor across `src` and `scripts`, a typecheck job, and the Worker suite
on Node.js 22, for pull requests and pushes to `main`. Workflow permissions are
read-only, actions are pinned to full commit SHAs, checkout does not persist
credentials, and superseded pull request runs are cancelled. This verifies local
behavior, not a live Discord connection or production deployment.

### Submitting changes

1. Fork the repository
2. Create your feature branch
3. Commit your changes
4. Push to the branch
5. Create a Pull Request
