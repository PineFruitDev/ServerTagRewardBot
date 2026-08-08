# CLAUDE.md

Orientation for any Claude session working in this repo. Keep it tight; link out rather than inline.

## Project

ServerTagRewardBot, a public multi-guild Discord bot that grants a reward role while a member has
your server's tag equipped and removes it when they unequip. discord.py, built on the
[PyTemplateBot](https://github.com/PineFruitDev/PyTemplateBot) architecture. Public repo under
`PineFruitDev`.

The mechanism: Discord exposes the equipped tag on the User object as `primary_guild`. The bot
watches raw `GUILD_MEMBER_UPDATE` gateway dispatches rather than the parsed member events, because
the parsed events do not reliably carry that field. Per-guild config lives in a local SQLite store.

## Heads up: there is a TypeScript port, and it is not here

A TypeScript rewrite of this bot exists at `Bots/ServerTagRewardBot` in Sky's local development
tree, ported onto the TSTemplateBot architecture. **It has no git remote and has never been
pushed.** This Python repo is the one that is actually published and hosted.

Before you add a feature, check whether the TS port already has it, and be explicit in the PR about
which of the two you changed. Do not assume the two are in sync, because they are not.

## Commands

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python register.py   # register slash commands with Discord
python main.py       # start the bot
```

No test suite is configured. If you add one, use `pytest`, put it in `test/`, and add the dependency
to `requirements.txt`.

Environment (`.env.example`): `DISCORD_TOKEN` (required), `DEVELOPER_IDS`, `ENVIRONMENT`,
`SYNC_ON_START`, `SYNC_DELAY_MS`.

## Deploy model (Sparked Host / Pterodactyl)

Runs on a Pterodactyl Python egg with the startup locked to `git pull`, dependency install, then the
entrypoint.

- **Boot does a `git pull`.** Whatever is on `main` is what runs, so `main` must always be bootable.
- **Secrets live only in the panel**, on the Startup tab or in `/home/container/.env`. Never commit
  a token and never put a real value in `.env.example`.
- Command registration is a separate `register.py` step, not automatic on boot.
- The SQLite store is runtime state. It is gitignored, and a boot sequence that wipes untracked
  files loses per-guild config. `/setup` repairs a guild in place.

## Rate limits and member syncing

This bot walks guild members and re-fetches users whose payloads omit `primary_guild`. That is the
part most likely to get the bot rate limited or flagged as it grows across servers. `SYNC_DELAY_MS`
exists for exactly this reason. **Do not remove or lower the throttle to make a sync finish faster**
without thinking about the largest guild the bot is in, not the one you are testing against.

## Licensing (read this before assuming)

This repo is **MIT**, not Apache 2.0 with the Commons Clause. That is out of step with the rest of
the public PineFruit set (`TSTemplateBot`, `VoiceLoungeBot`, `Free-Verse` are Apache 2.0 plus
Commons Clause, which permits use and forking but forbids selling the library itself).

**MIT permits selling.** If the "use, fork, do not sell" intent is meant to apply here, the LICENSE
file has to change; documenting the intent is not enough. Do not relicense on your own initiative,
and do not describe this repo as "no selling" while the MIT text says otherwise. Raise it with Sky.

Branch protection on `main`: PR required, admins included, zero approvals needed.

## House rules

- **Never commit to `main`.** Branch, open a PR, squash-merge with `--delete-branch`. This holds for
  one-line doc fixes too.
- **No em dashes** anywhere: code, comments, docs, commit messages, PR bodies, UI strings, embed
  copy. Use a comma, a colon, or parentheses.
- **No AI-collaboration tells.** No `Co-Authored-By` trailer, no "Generated with", no mention of
  Claude or any assistant in commit messages, PR bodies, code comments, or docs. This repo is public
  and part of Sky's portfolio; it reads as his own work.
- **Check the other repos before building something new.** `PyTemplateBot` is the upstream
  architecture and `TSTemplateBot` is its sibling. The TS port of this bot is prior art too.
- Stage explicit paths. No `git add -A` and no `git add .`.

## Constraints

- Do not commit secrets or a populated `.env`.
- Do not weaken the sync throttle to speed up a test.
- Do not change the reward-role grant or removal logic without checking the role-position guard;
  the bot's role must sit above the role it hands out, and `/setup` checks that for a reason.
