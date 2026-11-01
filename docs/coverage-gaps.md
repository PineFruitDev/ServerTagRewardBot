# Coverage gaps

What the test suites measure, what they do not, and why. Written so that the
next person to widen the scope or move the floor can see which gaps are
deliberate and which are just unfinished.

Measured on 2026-11-01 against `6d7c727`, with the repo's own tooling and no
extra instrumentation.

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
