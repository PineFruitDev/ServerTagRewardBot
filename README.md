# ServerTagRewardBot

Reward your community for repping your server. This public bot grants a role the moment a member equips your server's tag and removes it when they stop, keeping tag adoption and role perks perfectly in sync. Invite it, run `/setup`, done.

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

## Self-Hosting

### 1. Setup

```bash
git clone <your-repo>
cd ServerTagRewardBot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### 2. Configure

Edit `.env` with your bot credentials:

```env
DISCORD_TOKEN=your_bot_token_here
DEVELOPER_IDS=your_user_id_here  # Optional
ENVIRONMENT=development          # Optional
SYNC_ON_START=true               # Optional
SYNC_DELAY_MS=350                # Optional
TAGBOT_STORE_URL=https://tagbot-store.<you>.workers.dev
TAGBOT_STORE_KEY=<bearer secret, same as the Worker secret>
MEMBERS_INTENT=true              # Optional
```

> **Required Discord setup:** enable the **Server Members Intent** on the Bot page of the [Developer Portal](https://discord.com/developers/applications) and invite with the **Manage Roles** permission.
>
> **Limited mode:** if Discord refuses the Server Members intent (close code 4014, e.g. while a verified bot's intent request is pending), the bot logs a warning and reconnects without it. Slash commands keep working from the store; live tag sync and role updates stay off until the intent is granted and the bot restarts. Set `MEMBERS_INTENT=false` to start in limited mode directly.
>
> **Storage:** deploy the Worker in `worker/` (see `worker/wrangler.toml`), then set `TAGBOT_STORE_URL` and `TAGBOT_STORE_KEY`. `scripts/migrate_sqlite_to_d1.py` imports a legacy `data.db`; `scripts/smoke_test.py` checks a running store.

### 3. Deploy

```bash
python register.py   # Register commands with Discord
python main.py       # Start the bot
```

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
    ├── guild_config.py       # Per-server reward roles (tagbot-store)
    ├── streaks.py            # Streak tracking (tagbot-store)
    ├── logger.py             # Structured JSON logging with credential redaction
    ├── discord_errors.py     # Classifies Discord API failures (fixable vs transient)
    └── environment.py        # Config validation
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

| Variable | Required | Description |
|----------|----------|-------------|
| `DISCORD_TOKEN` | ✅ | Bot token from Discord Developer Portal |
| `DEVELOPER_IDS` | ❌ | Comma-separated user IDs for developer commands |
| `ENVIRONMENT` | ❌ | Environment mode (defaults to production) |
| `SYNC_ON_START` | ❌ | Sync all configured servers at startup (defaults to false; /setup handles first sync) |
| `SYNC_DELAY_MS` | ❌ | Throttle between user re-fetches during sync (defaults to 350) |
| `TAGBOT_STORE_URL` | ✅ | HTTPS URL of the tagbot-store Worker |
| `TAGBOT_STORE_KEY` | ✅ | Bearer secret for the Worker (keep in a secrets manager) |
| `MEMBERS_INTENT` | ❌ | Request the Server Members intent (defaults to true; falls back to limited mode if refused) |

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
npm --prefix worker test
```

The Python suite covers tag detection, role reconciliation, pagination and sync
failures, slash command wiring and guards, the post-setup sync prompt,
configuration, storage retries and caching, streak math, and the one-time
SQLite-to-D1 migration. Discord calls are mocked; HTTP client tests use a
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
