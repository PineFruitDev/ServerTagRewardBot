# ServerTagRewardBot

Reward your community for repping your server. This bot grants a role the moment a member equips your server's tag and removes it when they stop, keeping tag adoption and role perks perfectly in sync.

Built on the [PyTemplateBot](https://github.com/PineFruitDev/PyTemplateBot) architecture: command class pattern, single source of truth, discord.py.

## Features

- **Live Tag Detection**: Reacts within seconds when members equip or remove your server tag
- **Automatic Role Sync**: Grants the reward role on equip, removes it on unequip, no manual work
- **Startup Sync**: Full member reconciliation at boot so existing tag-wearers get their role on day one
- **Admin Commands**: `/sync` for manual reconciliation, `/status` for live statistics
- **Environment Validation**: Comprehensive startup checks with helpful error messages
- **Production Ready**: Error handling, rate-limit friendly syncing, and clean architecture

## How It Works

Discord exposes a member's equipped server tag on the User object as `primary_guild` (`{ identity_guild_id, identity_enabled, tag, badge }`). A member is repping your tag when `identity_guild_id` matches your server and `identity_enabled` is true.

The bot watches raw `GUILD_MEMBER_UPDATE` gateway dispatches, which fire when that field changes, so role updates land in near real time. A full sync walks every member and re-fetches users whose payloads omit the field, throttled to stay friendly with rate limits.

## Quick Start

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
GUILD_ID=your_server_id_here
TAG_ROLE_ID=role_to_grant_here
DEVELOPER_IDS=your_user_id_here  # Optional
ENVIRONMENT=development          # Optional
SYNC_ON_START=true               # Optional
SYNC_DELAY_MS=350                # Optional
```

> **Required Discord setup:** enable the **Server Members Intent** on the Bot page of the [Developer Portal](https://discord.com/developers/applications), invite the bot with the **Manage Roles** permission, and place the bot's role above the reward role in Server Settings > Roles.

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
│   ├── ping.py               # Latency check
│   ├── status.py             # Sync statistics
│   ├── sync.py               # Manual full sync (admin)
│   └── help_command.py       # Auto-generated help
└── services/
    ├── tag_sync.py           # ← Tag detection + role sync engine
    ├── logger.py             # Contextual logging
    └── environment.py        # Config validation
main.py                       # Entry point
register.py                   # Command registration
```

## Built-in Commands

- `/status` - Sync statistics: members checked, roles added/removed, live updates seen
- `/sync` - Manual full member sync (requires Manage Server)
- `/ping` - Basic ping/pong with latency
- `/help [command]` - Auto-generated help system

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DISCORD_TOKEN` | ✅ | Bot token from Discord Developer Portal |
| `GUILD_ID` | ✅ | The server whose tag counts |
| `TAG_ROLE_ID` | ✅ | The role to grant while the tag is equipped |
| `DEVELOPER_IDS` | ❌ | Comma-separated user IDs for developer commands |
| `ENVIRONMENT` | ❌ | Environment mode (defaults to production) |
| `SYNC_ON_START` | ❌ | Run a full member sync at startup (defaults to true) |
| `SYNC_DELAY_MS` | ❌ | Throttle between user re-fetches during sync (defaults to 350) |

## Notes

- Your server needs the Server Tag feature enabled for members to equip a tag
- The bot only ever manages the one configured role
- Large servers: startup sync is throttled; expect roughly 3 members/second when user re-fetches are needed

## License

MIT License - See LICENSE file for details.

## Contributing

1. Fork the repository
2. Create your feature branch
3. Commit your changes
4. Push to the branch
5. Create a Pull Request
