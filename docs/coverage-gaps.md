# Coverage gaps

What the test suites measure, what they do not, and why. Written so that the
next person to widen the scope or move the floor can see which gaps are
deliberate and which are just unfinished.

Measured on 2026-11-01 against `6d7c727`, with the repo's own tooling and no
extra instrumentation.

Two passes are recorded here. The first closed the gaps that line coverage
could see and is everything up to "If you are here to change the floor". The
second, in [Second pass: behavioural gaps](#second-pass-behavioural-gaps-at-100-line-coverage),
starts from the conclusion of the first (every module in `src/` at 100%) and
asks the question percentages cannot: would any test notice if the code were
wrong?

## How to reproduce

```bash
# Python, as CI runs it
python -m pytest -q --cov=src --cov=scripts --cov=main --cov=register \
  --cov-branch --cov-report=term-missing --cov-fail-under=97

# Worker
npm --prefix worker ci
npm --prefix worker test
```

To see a single module's gaps, narrow the scope: `--cov=main` on its own prints
only `main.py`.

## Headline

`src/` was already at or near 100% on every module, so the honest finding is
that the gaps were not inside the package at all. They were in what the
measurement could not see.

Before this pass, CI measured `--cov=src --cov=scripts`. That excluded both
entry points, `main.py` and `register.py`, because they sit at the repository
root rather than under `src/`. The 93% floor was therefore reporting a healthy
number while one production module had **no tests at all** and nothing in CI
could say so. The floor also carried 4.7 points of slack (actual 97.73% against
a 93% gate), so a real regression could have landed without tripping it.

Two changes close that: the scope now includes `main.py` and `register.py`, and
the floor is 97% against an actual 97.73% on Python 3.13.

Statement and arc totals shift slightly between interpreters, so the headline
figure does too: 97.73% on 3.13, 97.71% on 3.15. Both clear 97%. The margin is
deliberately thin, so a change that adds an uncovered branch is meant to have to
justify itself rather than be absorbed by slack.

## Ranked gaps

Ranked by what a failure in the untested path would actually cost, not by line
count. Items 1-5 are addressed in this change; 6-8 are recorded as accepted.

Two of the five were not merely untested: writing the missing cases turned up
real defects, in both cases a validation or redaction rule that was narrower
than the rule it sat next to. That is the usual shape of a coverage gap worth
closing, and the argument for ranking by cost rather than by percentage.

### 1. `register.py`: 0% covered, and outside the measured scope

The worst of the two blind spots. 25 statements, zero tests, and no test
imported it, so `--cov=register` reported "No data to report".

It is the hand-run script that pushes the command tree to Discord after a
command changes. Its `on_ready` closes the connection inside a `finally`, which
is the one property that matters and the easiest to delete by accident: if
`sync_commands()` raises and `close()` does not run, the script never hangs up.
The operator gets a terminal that never comes back instead of a stack trace,
and the usual reaction is to Ctrl-C and assume the registration worked.

Also unpinned: that `Environment.validate()` runs before anything connects,
that `log_handler=None` is passed so discord.py does not replace this bot's JSON
logging, and that registration asks for default intents only (requesting the
privileged members intent here would fail with close code 4014 on any bot not
yet approved for it, for no benefit, since pushing the tree is an HTTP call).

Covered by `tests/test_register_script.py`. Now 93%; the remainder is the
`if __name__ == "__main__":` guard.

### 2. `main.py` `main()`: the process exit contract, 0% covered

Lines 88-116 were entirely untested, and `main.py` was outside the scope too.

`main()` is the part of the bot a supervisor interacts with, and its exit codes
are a contract. systemd and Docker restart policies read them:

| Condition | Required exit | Why it matters |
| --- | --- | --- |
| `discord.LoginFailure` | 1 | A reset token must not look like a clean run, or a restart loop reads as healthy |
| `discord.PrivilegedIntentsRequired` | 1 | Same, for a refusal that survived the limited-mode fallback |
| `KeyboardInterrupt` | 0 | A deliberate stop is not a crash |
| Normal return | 0 | n/a |

Nothing pinned any of these, nor that "Shutdown complete" is *not* logged on
the failure paths (claiming a clean shutdown after an aborted start hides the
failure in exactly the logs someone would check first).

Startup order was also unpinned: validate, then adopt discord.py's loggers, then
build the store-backed singletons, then connect. The logging step has to precede
the connection or the library's first rate-limit line arrives unredacted.

Covered by `tests/test_main_entrypoint.py`. Now 98%; the remainder is the
`__main__` guard.

### 3. `main.py` line 57: the `sync_on_start` branch

The only uncovered line inside a module that was already in scope. A full sync
at boot walks every member of every guild, so the branch that decides whether
to launch it is worth pinning in all three states: on with the members intent
(one supervised `startup-sync` task), off by default (no task, because the
initial sync belongs to `/setup`), and on without the members intent (no task,
because there is no tag data to sync).

Covered in `tests/test_main_entrypoint.py`.

### 4. `src/services/logger.py` `49->54`: a redaction bypass behind a partial branch

A single uncovered branch, and the highest-value one in the report. Line 49 is
`if isinstance(record.args, tuple)`, so the uncovered arc was its *false* side:
a log record whose `args` is not a tuple.

`JsonFormatter` promises, in its own class docstring, never to serialize an
exception message, because an exception's `str()` is whatever a library put in
it: a response body, a rejected URL, a credential. It delivers that by
rewriting exception arguments to their type name. But it only did so for tuple
args, and `logging` unwraps a lone mapping argument, so `%(name)s` formatting
arrives as a dict and skipped the rewrite entirely:

```python
log.error("request failed (%(exc)s)", {"exc": RuntimeError("response body")})
# before: {"message": "request failed (response body)"}
# after:  {"message": "request failed (RuntimeError)"}
```

Latent rather than live: no call site in the repo formats that way today, which
is exactly why the branch was uncovered. But a redaction control that depends on
which `%`-style the caller picked is one `log.error` away from breaking a stated
guarantee, and this is the module every other module logs through.

Fixed by scrubbing mapping args the same way, building a new dict so the
caller's own mapping is never mutated. The branch tests `isinstance(..., Mapping)`
rather than `dict`: `logging` accepts any `Mapping`, so a `MappingProxyType` or
a `UserDict` would otherwise slip through unscrubbed and leave the same hole one
type narrower. Tested across all three mapping types, and for `args is None`
(the `log.info("Shutdown complete")` shape, which is the most common call in the
codebase and had never been through the formatter). `logger.py` is now at 100%.

### 5. Worker `now` validation: untested, and wrong

`worker/test/store.test.mjs` covered an *omitted* `now` falling back to the wall
clock, but not a present-but-unusable one. Writing those cases found a real
defect: the guard was `Number.isInteger(body.now)`, and `Number.isInteger(1e21)`
is `true` in JavaScript, so `1e21` passed validation and was stored verbatim as
`active_since`.

The effect is a wedged member rather than data loss. `streaks.py` computes
`max(0, now - active_since)`, so the streak reads as active with zero duration
until something marks them not-repping. Low severity, and unreachable from the
bot itself, which always sends `int(time.time())`. But it contradicted the
module's own stated policy: `snowflake()` screens IDs with
`Number.isSafeInteger` for precisely this class of mistake.

Fixed by using `Number.isSafeInteger`, with cases for zero, negative,
fractional, string, null and out-of-range timestamps, plus an assertion that a
rejected timestamp cannot inflate a total when the streak closes, and one that
a usable timestamp is still stored exactly. The 404 table also gained the route
shapes that were previously unexercised (`/streaks`, `/guilds/:id/unknown`,
`GET /guilds/:id/disable`, `PUT /streaks/:id/mark`, `/health/:id`, `/`).

Scope of that fix, stated precisely because the first draft of this section
overstated it: `isSafeInteger` screens values that cannot round-trip through
JSON as an integer. It does **not** screen implausible ones. A future timestamp
inside the safe range, say `now` a year ahead, is still accepted and still
produces the active-with-zero-duration reading until the member is marked
not-repping. Bounding that means picking a clock-skew tolerance, which is a
policy decision with its own failure mode (rejecting legitimate marks from a
host with modest drift) and does not belong in a coverage pass. Recorded here
rather than quietly fixed with an arbitrary constant.

### 6. Accepted: the Worker has no coverage instrumentation

`npm test` reports pass/fail only. Node's `--experimental-test-coverage` cannot
help here, because the Worker executes inside workerd via Miniflare, in a
separate process from the test runner, so the instrumented module is never the
one that runs.

Left as is. The suite drives the real HTTP routes against real local D1 and,
read against the 189-line source, covers every route, every validation branch
and both `markStatement` arms. A number would be reassuring but adding one means
either a second execution path for the Worker (which would measure something
other than what ships) or a build step. The source is small enough to audit by
reading; this document is the audit.

### 7. Accepted: `scripts/smoke_test.py` write path, 51%

Lines 57-88 are the `--write` checks, and they stay untested on purpose. The
reasoning is already in `tests/test_smoke_script.py`: faking the store in Python
would be a second implementation that drifts from the Worker and vouches for
nothing. The behaviour it would assert is covered against real D1 by
`worker/test/store.test.mjs`, and the script's read-only path, its pass/fail
reporting, and its promise never to print the store key are all tested offline.

This is the one place where a low number is the right number. Worth knowing
when reading the report, which is why it is here rather than hidden behind a
coverage exclusion.

### 8. Accepted: three residual partial branches

| Location | Branch | Disposition |
| --- | --- | --- |
| `src/services/environment.py` | `209->215` | Unreachable. `_unusable` is only called from `_check_required`, which iterates `REQUIRED_VARS`: exactly the three names the function handles. Defensive fallthrough, not a gap. |
| `src/services/lifecycle.py` | `90->exit` | The gateway task finishing cleanly *during* the drain window. A timing-dependent interleaving, and the failure case (the one that could swallow an error) is tested. |
| `scripts/migrate_sqlite_to_d1.py` | `128` | `if __name__ == "__main__":` guard. |

The `__main__` guards in `main.py`, `register.py` and
`scripts/migrate_sqlite_to_d1.py` are each one uncovered dispatch line. No
coverage exclusion was added for them: a `.coveragerc` exclusion would also
hide `scripts/smoke_test.py`'s lines 94-96, which contain real argparse logic,
and inflating the headline number is not worth losing that.

## Results

| Module | Before | After |
| --- | --- | --- |
| `register.py` | 0%, unmeasured | 93% |
| `main.py` | 74%, unmeasured | 98% |
| `src/services/logger.py` | 98% | 100%, one defect fixed |
| `worker/src/index.js` | 34 tests, unmeasurable | 42 tests, one defect fixed |
| Python total | 97.73% over `src` + `scripts` | 97.71% over `src` + `scripts` + both entry points |
| Python tests | 533 | 555 |
| CI floor | 93% (4.7 points of slack) | 97% (0.71 points of slack) |

Both Python totals in that row are measured on the same interpreter (3.15) so
they are comparable; on 3.13 the new figure is 97.73%, and the slack is the
thinner of the two.

The total moves slightly down because the scope grew to include two modules
that were previously contributing nothing to the denominator. That is the point:
it now measures more, so it is a number worth gating on.

Verified on Python 3.13 as well as 3.15 before merge, because 3.12 and 3.13 are
what CI runs and a thin margin is worth checking on the interpreter that gates
the branch rather than only the one to hand.

## If you are here to change the floor

Raise it when coverage rises; do not lower it to make a change fit. The two
defects above were both sitting behind the gap between the floor and the actual
figure, and neither would have been found by a suite that only had to clear 93%.

Before narrowing `--cov`, check whether the module you are dropping is one of
the entry points. Those are the ones that were invisible for long enough to
grow a module with no tests at all.

# Second pass: behavioural gaps at 100% line coverage

Measured on 2026-11-01 against `e1b2e68`. The first pass ended with every
module in `src/` at 100% statement and branch coverage, which retires line
coverage as a way to find anything else. A covered line is only a line that
ran; it is not a line whose behaviour anything checked.

So this pass asks the other question directly: change the code so it is wrong,
and see whether the suite fails. A change that leaves the suite green is a
behaviour no test pins, and the report below is the list of those.

## How to reproduce

```bash
python -m pytest -q --no-header            # 566 tests
npm --prefix worker ci && npm --prefix worker test   # 57 tests
```

Then, for any single claim in the tables below, apply the stated edit to the
stated file, re-run the suite for that side, and confirm it now fails. 86
edits were tried: 52 against the Python modules and 34 against the Worker.

No tooling was added for this. A general mutation runner (`mutmut`,
`cosmic-ray`) would be a new dev dependency inside the `pip-audit --strict`
gate, and the useful part was not the breadth but choosing edits that
correspond to a mistake somebody could actually make. The edits are written
out in full so the pass is repeatable by hand.

### A trap worth knowing about

The first run of the Python half reported 4 surviving edits. The real number
was 18. CPython validates a cached `.pyc` against the source's modification
time and size, so a same-length edit that is reverted quickly (`MARK_BATCH =
51` back to `MARK_BATCH = 50`, or `< 60` back to `< 59`) can leave stale
bytecode that still gets imported. Later runs were then measuring the previous
edit rather than the current one, which turns surviving edits into false
"caught" results.

Anyone repeating this needs `PYTHONDONTWRITEBYTECODE=1` and a cleared
`__pycache__` between edits. The symptom to watch for is the one that exposed
it: `grep MARK_BATCH src/services/streaks.py` printing 50 while
`python -c "from src.services.streaks import MARK_BATCH; print(MARK_BATCH)"`
prints 51.

## Headline

Both suites are in good shape. 61 of the 86 edits were caught before this
change, including every edit to a command handler, to the permission guard, to
the cache-revision fencing in `guild_config.py`, and to the streak arithmetic
in `summarize()` and `rank()`. The command-handler layer in particular is
comprehensively pinned: all 7 edits there were caught, so the handlers are not
where the remaining risk is, which is why this change adds no tests there.

25 edits survived the first measurement (18 Python, 7 Worker). They split four
ways, and the split matters more than the count:

| Disposition | Count | Meaning |
| --- | --- | --- |
| Real gap, now closed | 13 | A behaviour that mattered and that nothing checked |
| Equivalent edit | 6 | The "wrong" code behaves identically, so no test can tell |
| Accepted | 3 | Real, but not worth a test or not observable from one |
| Deferred | 3 | Belongs to open PR #26, which is rewriting that code |

So 13 of the 25 were real, and 12 edits still survive by design. After this
change 74 of the 86 edits are caught.

Nothing in this pass changed production code. Every finding was a missing
test, and the one case where production looked less defensive than the edit
(`{"rows": null}`, below) is recorded rather than quietly changed.

## 1. The longest-streak guarantee, unpinned

The most valuable finding, and the one with a user-visible failure mode.

`worker/src/index.js` closes a streak with:

```sql
longest_seconds = MAX(longest_seconds, MAX(0, ?1 - active_since)),
```

Dropping the outer `MAX(longest_seconds, ...)`, so that closing a streak
simply overwrites the record, left both suites green.

The existing streak test walks 50 seconds and then 75 seconds. Both closes set
a new best, so the guard is never asked to defend an older record, and the
assertion passes whether or not it exists. The case that distinguishes them,
a long streak followed by a short one, was absent.

The effect on a live server: `longest_seconds` is what `/repped` prints as
"Longest Streak" and what `/leaderboard sort:longest` ranks on. A member with
a 10-day record who equipped the tag and removed it an hour later would watch
their record drop to an hour, with no way to recover it, because the previous
value is gone from D1.

Production is correct. This was only ever a missing test, verified by reading
the real value back out of local D1 before and after:

```
after a 10-day streak closes: total 864000, longest 864000
after a 1-hour streak closes: total 867600, longest 864000   <- record held
```

Closed by two cases in `worker/test/store.test.mjs`: a shorter streak closing
after a longer one, and the related guarantee that a running streak which
beats the record is not banked into `longest_seconds` until it closes.

## 2. Types on the way to the store

A cluster of five edits, all surviving, all the same shape: delete a `str()`
or `bool()` conversion on the boundary between the bot and the Worker.

| File | Edit | Survived because |
| --- | --- | --- |
| `streaks.py` | `str(uid)` to `uid` | Every caller already passes strings |
| `streaks.py` | `bool(rep)` to `rep` | Every caller already passes real bools |
| `guild_config.py` | drop `str(guild_id), str(role_id)` | Same |
| `guild_config.py` | drop `str(g), str(r)` in `all_guilds` | Same |
| `streaks.py` | drop `str(row["user_id"])` in `rank` | Same |

These conversions are not cosmetic. The Worker screens ids with
`Number.isSafeInteger` and requires `typeof repping === "boolean"`, so an int
or a truthy non-bool that reaches it is a 400. Worse, a real 19-digit
snowflake sent as a JSON number loses precision in transit, before the
Worker's screen can see it, which is the exact mistake the Worker's own
comment says `snowflake()` exists to catch. The bot's conversions are its half
of that contract and nothing held them in place.

Closed in `tests/test_stores.py` by passing int guild ids, int snowflakes and
truthy/falsy non-bools through `mark_many`, `set_role`, `all_guilds` and
`rank`, and asserting both the values and their types on the way out.

## 3. `mark()` never tested with `repping=False`

`StreakStore.mark()` is the single-member entry point. Both of its existing
tests pass `repping=True`, so hardcoding `True` inside it left the suite green.

Scope stated precisely, because it is narrower than it first looks: nothing in
`src/` calls `mark()`. Production records streaks through `mark_many()`, from
`tag_sync._record_streaks`. So this is a gap in public API that only tests
currently exercise, not a live defect.

It is worth pinning anyway, because `mark()` is what a live equip/unequip
handler would reach for, and the failure mode on that path is a streak that
can be started and never closed. Closed in `tests/test_stores.py`.

## 4. Smaller gaps, closed

| Finding | Edit that survived | Now pinned by |
| --- | --- | --- |
| The guild listing's order was unspecified. `all_guilds()` feeds the startup sync, and `guild_id` is TEXT, so the order is lexicographic ("200", "30", "4"), not numeric. The existing test's two guilds happen to come back in insertion order either way. | drop `ORDER BY guild_id` | `store.test.mjs` |
| The auth scheme check could be removed entirely. `slice(7)` strips a 7-character scheme either way, so a lowercase `bearer ` was the only thing that could tell the difference. | `startsWith("Bearer ")` to a truthiness check | `store.test.mjs` |
| 400 responses asserted only their status, never which guard produced them. A JSON array passes `readJson`'s `typeof body === "object"` check, because arrays are objects in JavaScript, and is caught downstream instead. | `typeof` check to a truthiness check | `store.test.mjs` |
| The day boundary in `humanize()` could be off by one second. 86400 renders as "1d" under both the correct divisor and an 86399 one; only 86399 itself distinguishes them. | `divmod(seconds, 86400)` to `86399` | `test_stores.py` |
| `disable()` returned the store's raw value where its annotation promises `bool`. | drop `bool(...)` | `test_stores.py` |
| `leaderboard()`'s default limit of 10 was never exercised, since `/leaderboard` passes it explicitly. | `limit: int = 10` to `3` | `test_stores.py` |

## 5. Equivalent edits: no test could catch these

Worth recording so the next person does not spend time trying. In each case
the "broken" code behaves identically to the correct code, so a failing test
is not available.

| Edit | Why it cannot be caught |
| --- | --- |
| `humanize`: `if seconds < 60` to `< 59` | 59 falls through to `days=hours=minutes=0`, and the `or "less than a minute"` fallback returns the same string. That fallback is load-bearing, not dead code. |
| `mark_many`: drop `guild_id = str(guild_id)` | The value is only ever used in an f-string, which calls `str()` anyway. |
| `summarize`: `if not row:` to `if row is None:` | `summarize({}, now)` returns the same zeros dict as `summarize(None, now)`, verified directly. |
| `get`: `data.get("row")` to `data.get("row", {})` | `{}` is falsy, so it reaches the same zeros path. |
| Worker: `count: stmts.length` to `marks.length` | `stmts` is `marks.map(...)`, so the lengths are equal by construction. |

One near-miss belongs here with a caveat. Changing
`data.get("rows", [])` to `data.get("rows") or []` in `leaderboard()` survives,
but the two are not identical: on a `{"rows": null}` response the current code
passes `None` into `rank()`, which raises `TypeError`, while the edit degrades
to an empty leaderboard. The edit is the more defensive of the two.

Left alone deliberately. The Worker always sends `{rows: results}` with an
array, so the response is not reachable today, and adding a fallback for a
shape the store does not produce is a behaviour change rather than a test,
which is the same line the first pass drew around clock-skew tolerance. It is
recorded here so the choice is visible.

## 6. Accepted

| Finding | Disposition |
| --- | --- |
| Worker: replacing `crypto.subtle.timingSafeEqual` with a plain byte comparison survives. | Correct, and not fixable by a functional test: timing is not observable from the test runner. The observable half, that both sides are hashed so lengths always match, is now pinned by a case that sends a wrong key of exactly the right length. |
| Worker: `(res.meta && res.meta.changes) > 0` to `res.meta.changes > 0` survives. | Defensive against a driver that omits `meta`. Local D1 always provides it, so there is no way to reach the guard without faking the driver, which the first pass already rejected as vouching for nothing. |
| `guild_config.py`: `_CACHE_TTL = 60.0` to `600.0` survives. | A tuning constant. Every test sets `cache_ttl` explicitly, which is the right thing for a test to do; pinning the module default would mean asserting on a private attribute to no benefit. |

## 7. Deferred to PR #26

Three surviving edits are in `store_client.py`'s `Retry-After` handling:

| Edit | Unpinned behaviour |
| --- | --- |
| `requested_wait > _MAX_RETRY_AFTER` to `>=` | A wait exactly at the 30s cap |
| `max(0.0, float(raw))` to `float(raw)` | A negative `Retry-After` becoming a negative sleep |
| the per-attempt `requested_wait = None` reset | A `Retry-After` leaking into the wait after a later transport error |

Not closed here, deliberately. Open PR #26 moves this function out of
`store_client.py` into a new `src/services/backoff.py` so the Discord REST
paths can share it, which means a test written against the current location
would be written against code that is about to move, and would collide with
that PR in both `store_client.py` and `test_store_client.py`.

The third one is the most interesting of the three and the one to carry over:
`request()` resets `requested_wait` at the top of each attempt, with a comment
saying a `Retry-After` only governs the wait after the response that carried
it. The existing
`test_retry_after_only_governs_the_response_that_sent_it` does not actually
pin that, because its second response is another HTTP error that overwrites
`requested_wait` anyway. The reset only matters when the next attempt raises a
transport error instead, and no test covers that interleaving.

## Results

| Suite | Before | After |
| --- | --- | --- |
| Python tests | 555 | 566 |
| Worker tests | 42 | 57 |
| Python coverage | 97.73% | 97.73% |
| Edits caught (Python) | 34 of 52 | 43 of 52 |
| Edits caught (Worker) | 27 of 34 | 31 of 34 |

Python coverage does not move, and that is the expected result rather than a
disappointment: every gap in this pass was behind a line that already ran. A
pass that found real gaps without moving the percentage is the clearest
argument available that the percentage had stopped being the useful signal.

## If you are here to add tests

Check the equivalent-edits table first. Six of the twelve surviving edits
cannot be caught by any test, and three more are accepted for stated reasons,
so the honest remaining list is the three in PR #26's lane.

When adding a case to an existing sequence, check whether it distinguishes
anything. The longest-streak gap survived behind an assertion that looked
thorough: it asserted all four columns, on a sequence where two of them could
not have been wrong. A monotonically increasing sequence cannot test a
guard whose whole job is to defend the older value.
