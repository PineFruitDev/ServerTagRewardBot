# ServerTagRewardBot

Reward your community for repping your server. This public bot grants a role the moment a member equips your server's tag and removes it when they stop, keeping tag adoption and role perks perfectly in sync. Invite it, run `/setup`, done.

Built on the [PyTemplateBot](https://github.com/PineFruitDev/PyTemplateBot) architecture: command class pattern, single source of truth, discord.py.

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

The bot watches raw `GUILD_MEMBER_UPDATE` gateway dispatches, which fire when that field changes, so role updates land in near real time across every server it's in. Per-server reward roles live in a local SQLite store; a full sync walks members and re-fetches users whose payloads omit the field, throttled to stay friendly with rate limits.

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
```

> **Required Discord setup:** enable the **Server Members Intent** on the Bot page of the [Developer Portal](https://discord.com/developers/applications) and invite with the **Manage Roles** permission.

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
    ├── guild_config.py       # Per-server reward roles (SQLite)
    ├── streaks.py            # Streak tracking (SQLite)
    ├── logger.py             # Contextual logging
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

## License

MIT License - See LICENSE file for details.

## Contributing

1. Fork the repository
2. Create your feature branch
3. Commit your changes
4. Push to the branch
5. Create a Pull Request
