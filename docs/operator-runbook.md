# Operator runbook

Run repository commands from the root unless stated otherwise. This guide
covers the checked-in code, not changes proposed in other PRs.

## Deployment prerequisites

- Use Python 3.12 or 3.13 with the README virtual environment setup. Worker
  development uses Node.js 22 and the locked dependencies.
- Prepare a Discord application and bot with Server Members Intent enabled
  (and approved where required), Manage Roles, and access to slash commands.
  Put its highest role above the regular role selected for rewards.
- Prepare a Cloudflare Worker and D1 database. Review `worker/wrangler.toml`
  for your own account and database: the checked-in identifiers are not portable
  defaults. Preserve the `DB` binding and migration directory.
- Supply `DISCORD_TOKEN`, `TAGBOT_STORE_URL`, and `TAGBOT_STORE_KEY` privately
  to the bot. The Worker needs the same `TAGBOT_STORE_KEY` secret. Never put
  credentials in command arguments or commit them.
- Arrange a process supervisor that captures stdout and stderr and allows
  graceful shutdown. There is no checked-in container image or service unit.

The Worker package defines these commands. The remote commands modify live
infrastructure; use them only during an authorized deployment after confirming
its target and backup/recovery arrangements.

```bash
npm --prefix worker ci
npm --prefix worker run migrate:local
npm --prefix worker run dev
```

The development server stays running until stopped. For a separately authorized
remote release, apply the schema before starting a bot that depends on it:

```bash
npm --prefix worker run migrate:remote
npm --prefix worker run deploy
python register.py
python main.py
```

Provision the Worker secret through your deployment's private secret workflow
before starting the bot. `register.py` validates the same required environment
as the bot, syncs commands to Discord, and exits. The main process also syncs
commands on its first ready event. Registration is a live write, not a dry run.
There is no separate build script: Python runs directly and Wrangler bundles
the Worker during deployment. Use the README offline checks before release.

## Startup and recovery checks

Look for environment validation, then `Logged in` and the FULL or LIMITED mode,
and `Store reachable`. A successful login alone does not prove storage or role
updates work. Limited mode keeps commands available but disables tag syncing;
restore intent approval and restart to recover full operation.

In a designated test server, run `/setup`, choose the initial sync, and inspect
`/status`. Confirm an actual tag equip/removal changes the reward role. This is
live acceptance testing and is separate from the mocked/local test suites.
`/status` counters belong to the running sync service; they are not a durable
history of all operations. Guild configuration and streaks are stored in D1.

## Common failures

| Symptom | Interpretation and response |
|---------|-----------------------------|
| Startup configuration errors | Correct the named settings privately. Validation runs before connecting; format acceptance does not prove a credential is valid. |
| Discord rejects login | Check the configured bot token through the private credential workflow, then restart. |
| Close code 4014 / LIMITED mode | Server Members Intent was refused or disabled. Enable/obtain approval and restart; repeated sync requests cannot restore the intent. |
| HTTP 403 / Discord error 50013 | Check Manage Roles and bot role hierarchy. Full sync stops on a guild-wide role failure. Correct the permissions, then run one `/sync`. |
| Unknown role / Discord error 10011 | The configured role was deleted. Run `/setup` with an assignable replacement, then reconcile. |
| Discord HTTP 429 | Allow the library's rate-limit wait to finish. Avoid repeated `/sync` or restart loops. If the error escapes to the bot, a role write can fail or a member-list request can end the sync; reconcile once after recovery. `SYNC_DELAY_MS` throttles user re-fetches, not every Discord request. |
| Discord transient API failure | Wait for recovery, inspect the failed operation, and reconcile afterward. Do not assume a completion log proves every role write succeeded. |
| Store unavailable | Inspect `StoreClient` retries and Worker availability. The client uses four attempts by default, a five-second request timeout, jittered backoff, and Retry-After when supplied. A requested wait over 30 seconds fails immediately. |
| Store rejected request | Check the Worker target, shared secret configuration, D1 binding, and applied migrations privately. Non-retryable responses are not fixed by repeated requests. |
| Stale slash commands | Refresh Discord and retry. After command-definition changes, an operator can run `python register.py` against the intended application. |
| Sync already in progress | Wait and inspect `/status`; concurrent full syncs for one guild are skipped. |

Role changes and streak writes are separate operations. A failure can leave
partial progress; inspect logs and reconcile after recovery rather than assuming
an operation was atomic. `/disable` leaves existing roles intact.

## Reading logs

The application writes one JSON object per line to stdout, with `timestamp`
(UTC), `level`, `logger`, and `message`. `exception_type` appears when exception
information is attached. Startup validation can also write a plain-text exit
message to stderr, so capture both streams.

Use the timestamp to correlate events, the logger to identify the component,
and the guild ID in the message to follow one server. `TagSyncService` messages report
role writes, sync start/completion, and aborted member scans. `StoreClient`
reports retries and exhaustion. `CommandErrors` identifies command/button
failures and includes a source location for unexpected defects. During normal
bot startup, `discord` and `aiohttp` warning/error logs use the same JSON handler.

The formatter redacts known credential values and URLs and omits arbitrary
extras and tracebacks. Messages can still contain guild/user identifiers and
the configured store hostname. Review and minimize excerpts before sharing;
redaction is not permission to publish logs. Keep the application revision,
UTC interval, component, HTTP status/Discord code, and affected operation in an
incident note without copying credentials or full response bodies.

## Shutdown and rollback

Stop with Ctrl-C or the supervisor's SIGTERM. The lifecycle drains background
work for up to five seconds before cancellation, then closes the gateway and
store session. Allow additional time for close operations. After recovery,
reconcile interrupted syncs explicitly; startup syncing is optional.

To roll back application code, stop the process and restore a known-good
revision and its dependencies. Check compatibility with the current D1 schema
first. There is no automated database rollback command in this repository;
do not delete D1 or replay migrations as a substitute for a recovery plan.

## Source map

- Settings: `src/services/environment.py`
- Commands and access: `src/commands/`, `src/core/command.py`
- Startup/registration: `main.py`, `register.py`
- Sync and Discord failures: `src/services/tag_sync.py`, `discord_errors.py`
- Store retries: `src/services/store_client.py`
- Log format/errors: `src/services/logger.py`, `command_errors.py`
- Shutdown: `src/services/lifecycle.py`, `tasks.py`
- Worker commands/schema: `worker/package.json`, `worker/wrangler.toml`, `worker/migrations/`
