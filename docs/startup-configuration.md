# Startup configuration

Both `main.py` and `register.py` validate configuration before constructing clients or connecting. The first successful validation caches the parsed bot configuration; subsequent calls reuse it. Tools may read a preliminary configuration through `Environment.get_config()` before startup; startup validation never trusts that object and replaces it after all checks pass. Restart the process after changing deployment settings. Validation failures exit nonzero, list every affected key together, and never echo supplied values. No credentials are authenticated remotely by these checks.

Required settings:

- `DISCORD_TOKEN`: a dotted ASCII token string longer than 50 characters, using letters, digits, underscores, hyphens, and dots.
- `TAGBOT_STORE_KEY`: at least 32 printable ASCII characters without whitespace.
- `TAGBOT_STORE_URL`: an HTTPS endpoint with a valid host and optional port (1–65535). HTTP is allowed only on localhost or 127.0.0.1. Credentials, queries, fragments, whitespace, and backslashes are rejected. A path prefix is supported.

Optional settings keep their existing defaults when unset or blank:

- `ENVIRONMENT`: development or production, case insensitive; default production. Unknown modes now fail startup instead of warning and continuing.
- `SYNC_ON_START` and `MEMBERS_INTENT`: true/false, yes/no, on/off, or 1/0, case insensitive; default false and true respectively.
- `SYNC_DELAY_MS`: nonnegative whole milliseconds, default 350. Large accepted delays warn by key without showing the value.
- `COMMAND_COOLDOWN_SECONDS`, `GUILD_COMMAND_LIMIT`, `GUILD_COMMAND_WINDOW_SECONDS`, and `REWARD_COOLDOWN_SECONDS`: nonnegative whole integers, default 5, 20, 60, and 30 respectively. Zero retains its existing disable behavior.
- `DEVELOPER_IDS`: comma-separated 17–20 digit ASCII IDs greater than zero and within the unsigned 64-bit range. Surrounding whitespace and empty comma-separated entries are ignored; default empty.

All integer settings accept ASCII decimal digits through 2147483647. This bound prevents conversion and downstream timer arithmetic failures. Surrounding whitespace is trimmed. Malformed URL parser inputs and invalid integer encodings produce named configuration errors rather than raw parser exceptions. Successful startup messages also use key names without echoing values, hostnames, credential fragments, or configured numeric limits.

Deployment compatibility: unknown environment modes, embedded credential whitespace, malformed ports, URLs containing credentials/query/fragment components, Unicode numeric input, out-of-range IDs, and integer settings above the limit must be corrected before startup. Per-guild settings stored remotely are outside this process-environment validation.
