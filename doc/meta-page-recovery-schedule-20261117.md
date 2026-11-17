# Four-hour recovery and daily 10:00 report, 2026-11-17

User request: run Page recovery every four hours and broadcast once daily at
10:00. All schedule times use Asia/Shanghai, retaining the established :05 scan
minute: 00:05, 04:05, 08:05, 12:05, 16:05 and 20:05.

CPU: 43.166.187.96. Runtime: `/opt/meta-page-recovery/current`. Data, online SQLite
backup, frozen installer and activation manifest remain under
`/mnt/data-disk/meta-page-recovery`. Branch: `codex/meta-page-recovery-20261117` in
`gaoyuan12356416/ai-`.

## Behavior and boundary

- `meta-page-recovery-scan.timer` has the six daily execution times.
- `meta-page-recovery-report.timer` queues one daily report at 10:00. Neither
  timer catches up immediately on installation (`Persistent=false`). Its service
  delivers independently of an in-flight recovery batch. The worker can retry
  known rejections; an atomic outbox claim protects both owners against duplicate
  sends. Uncertain sends remain protected. No extra acceptance message is sent
  during this change.
- Each completed batch still freezes its results, but its notification row is
  `recorded`. The daily summary covers previous-day 10:00 through current-day
  10:00, deduplicates by recovery job and exposes the latest outcome when built.
  In-flight work and failed scans are included. Accepted Ad-update receipts in
  that interval are counted separately. Repeated retries do not multiply Ads.
- Daily report ID/UUID uses its Beijing date. Repeating the daily command keeps
  the original frozen body and sent/unknown receipts. Existing sent and unknown
  per-batch messages remain unchanged; only unsent legacy messages are withdrawn.
- Three pools, configured-ACTIVE Ad and parents, permissions, creative/media/
  tracking preservation, receipt reconciliation and local retries are unchanged.
  The unresolved PUR window choice is not answered by a frequency change:
  purchase>0 still uses each account's today plus yesterday. Reports explicitly
  distinguish that window's zero purchase from no returned insights.
- Earlier read-only audit of Page 1427784617077848 confirmed at least ten
  active Ads with 36 historical purchases (34 in Nov 11-17), missed by the
  two-day filter. This change does not replay skipped work or silently change
  purchase eligibility. Audit source remains in the data-disk `audits` directory.

## Deployment and verification

Push verified source to GitHub, then fetch the exact commit into the existing
CPU source checkout. Install to a new timestamped deployment directory with:

```
python3 ops/meta-page-recovery/deploy.py install --source CHECKOUT --commit SHA --deployment DEPLOYMENT
python3 DEPLOYMENT/deploy.py switch --deployment DEPLOYMENT
```

Install retains prior units, pointer, activation states, original portrait helpers,
and an online `state-before.sqlite3` backup. Switch does not invoke `activate`,
run a scan, rebuild the baseline, reset jobs or restore any database. Only the
recovery worker drains/restarts. The portrait collector and all organic publishers
retain their running versions and schedules.

Verify focused recovery/deployment tests, both `systemd-analyze calendar` values,
unit syntax, live enabled/active timers and their next triggers. Preview the daily
summary/card on a private state copy with blocked HTTP; no out-of-schedule group
message or Meta write is needed. Compare original baseline, jobs, Ad-attempt
receipts and existing message IDs before/after. The first actual 10:00 delivery
is a future scheduled event and must not be reported as already received.

## Exact rollback procedure

Use the new deployment's frozen installer:

```
python3 DEPLOYMENT/deploy.py rollback --deployment DEPLOYMENT
```

For a schedule update this restores previous code/unit files and enabled/active
worker/timer states, including the old hourly schedule when reverting this first
daily-report release. Pending/failed daily cards are retained as `recorded` before
returning to an old hourly sender; sent/unknown receipts are unchanged. Current
jobs, Page state, credentials, all attempts, accepted Ad changes and profile
snapshots remain intact. Never replace the live SQLite with `state-before.sqlite3`.
