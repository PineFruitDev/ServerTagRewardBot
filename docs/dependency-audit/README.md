# UGC dependency audit

Audit performed 2026-11-01 at 05:50 UTC against main 9157ec4.
Open PR #20 covers typing fixes, not dependency auditing. This change adds a
separate workflow and does not change its dependencies or application files.

## Results

- pip-audit 2.10.1, `-r requirements-dev.txt`: zero known vulnerabilities across
  27 resolved runtime and development packages. See audit-python.json.
- `npm audit --json` in worker: zero known vulnerabilities, including development
  dependencies. See audit-node.json.
- `pip list --outdated`: empty. `npm outdated`: only Miniflare, whose registry
  latest (4.20260730.0) is older than the existing 5.20261023.3-beta pin.
  No compatible non-major upgrades were available; manifests and lockfile are unchanged.

## Deferred and limitations

Keep the existing Miniflare prerelease pin: moving to registry latest would be a
major downgrade. Revisit when a compatible stable release is available. No known
CVE is deferred or suppressed. Python requirements are ranges, so this audit
covers the fresh resolution recorded in JSON, not every older allowed version.
The CI gate resolves and audits both runtime and development requirements on
Python 3.12 and 3.13 and audits the npm lockfile at all severity levels. Scanner
errors fail the job. Making these jobs required in branch protection remains a
repository administration setting, not a source change.

## Local validation

Python 3.15: 501 tests passed, 97.62% branch-inclusive coverage; Ruff and mypy
passed. Worker: 34 tests passed, syntax check passed, Wrangler dry-run bundle
passed (5.93 KiB). No deployment occurred. CI supplies the supported Python
3.12/3.13 checks; local results do not claim those versions were executed.
Known-advisory scans do not establish absence of undisclosed vulnerabilities.
