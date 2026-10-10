# X metric query limit and scheduler recovery

The X Auto metric reader expanded every candidate drama into one SQLite IN
clause. On the production SQLite 3.26 build, more than 999 bound parameters
raised `too many SQL variables` before selection, leaving tasks in retry_wait.

Read at most 400 dates and 500 content IDs per query in one read transaction.
Retain active-generation, platform, product, complete-window and content filters,
exact decimal strings, deduplicated inputs and the original global row order.
There is no publication-state migration or change to selection thresholds.

Validation:

```
python -m unittest scripts.test_x_auto_metric_query_limits scripts.test_x_auto_post_store scripts.test_x_auto_post_metrics scripts.test_x_auto_post_selector -q
```

The new tests enforce SQLite's 999-variable limit even on newer local builds,
cover 1,205 dramas, simultaneous 1,005-day / 1,105-drama filters, active-generation
selection, product/platform isolation, exact decimals, ordering and missing days.

Deployment is GitHub first. Run `scripts/install_x_auto_metric_query_limits.py`
with a full `--commit` and the exact live `--expected-release`. It overlays only
the changed core module and supporting tests/documentation on the existing
composite, drains Auto workers, backs up both databases with SQLite's online
backup API, verifies publication and token fingerprints, and restarts only the
Auto sidecar. Existing pool timer states remain unchanged during code deployment.

Rollback uses the same installer with `--rollback <backup-directory>`; it checks
the release identity and restores code only, preserving current databases and
tokens. An operational restoration of stopped pool timers is a separate action
under the user's publishing-repair authorization, following health and ledger
checks. Preserve configured accounts, frequency, frozen plans, historical failed
attempts, ambiguous outcomes and the existing 90-second schedule grace period.
Never invoke a historical catch-up or a synthetic Post to test this repair.

## Production acceptance, 2026-10-10 Beijing time

- Code commit: `5def5268635a6dbfd8148f86dd7ae9c47542323a`, pushed to
  `codex/x-publish-recovery-20261010` before deployment.
- CPU host: `43.166.187.96`; active link: `/opt/x-post-automation/current`.
- New release:
  `/mnt/data-disk/x-post-automation/releases/5def5268635a6dbfd8148f86dd7ae9c47542323a-metric-query`.
- Previous release:
  `/mnt/data-disk/x-post-automation/releases/23a9b466d781a592ed2e3da402ee2f97ac3c3a71-template-list-static`.
- Online database backups, immutable publication fingerprints, token hashes and
  operation manifest:
  `/mnt/data-disk/x-post-automation/maintenance/20261010-metric-query-5def5268`.
- Local Auto suite: 155 checks, one environment-dependent skip. Focused CPU
  suite: 63 passed. Python syntax and Git whitespace checks passed.
- The same live read that failed before the change now returns all 28,130
  active metric rows for 2,737 dramas. Query-only access was used.
- X Auto sidecar restart succeeded; local Auto, X OAuth and public OAuth health
  endpoints all returned HTTP 200. Code switching preserved publication/token
  fingerprints. Main API and X OAuth sidecar were not restarted.
- By 11:20, two natural Auto tasks had new confirmed Post IDs, each with one
  attempt and no unknown outcome. A subsequent audit found no new transient
  failure events after the repair. Older error text can remain on queued or
  selecting tasks until they advance; do not equate that with a new failure.
- Existing schedule, schedule-claim and manual timers were separately restored
  to active/enabled under the user's repair request. Their prior state and unit
  files are in `pool-timer-restoration/` inside the backup directory. The claim
  service succeeded and the next-day random plans were generated. Publish
  workers use their existing shared lock while Auto drains its pending work.
- Existing pool configuration remains 18 configured accounts and one daily
  batch per pool. The next material slot is 2026-10-10 16:20; the next future
  drama slot is 2026-10-11 01:04. The elapsed 08:25 drama slot was not redrawn.

Yesterday's quota shortfall was distinct from today's SQL error: on October 9
the two stopped pool timers had not created the daily plans (122 target posts),
while Auto published 12 and had three no-candidate results under its configured
ROAS/media/history gates. The pool triggers were stopped during the October 1
credit-exhaustion incident and had not been restored. Current confirmed Auto
Posts demonstrate the shared API can publish again; this is not a balance audit.

Remaining scope is explicit: 15 bound dramas retain known failed episodes from
the old billing incident. Historical retry is awaiting the user's scope choice;
their queue/log/pool facts remain unchanged. One unknown-result account remains
fenced, and another configured account was freshly confirmed suspended by X.
These are account-local holds, not a reason to stop healthy accounts. Do not
describe the entire drama pool as fully restored while these holds remain.

The local `ai-backend-maintenance` skill context was updated with the reusable
query-limit, snapshot and timer-restoration guidance.

Code-only rollback, after the installer safely drains workers:

```bash
python3 /mnt/data-disk/x-post-automation/install-metric-query-5def526.py --rollback /mnt/data-disk/x-post-automation/maintenance/20261010-metric-query-5def5268
```

This preserves current publisher databases, tokens and pool timer states. To
separately undo this task's restoration of the pool triggers, stop only these
timers (do not interrupt an in-flight sidecar request):

```bash
systemctl stop x-post-schedule.timer x-post-schedule-claim.timer x-post-manual.timer
```
