---
name: meta-violation-material-cleanup
description: Maintain, inspect, deploy, and reconcile the twice-daily CPU Meta violation-material cleanup for DramaWave and HotDrama, including its rules, shared deletion ledger, and Feishu notifications to 郜远. Use for this scheduled cleanup, not the separate 2446289 replacement-Creative repair job or ordinary ad-performance control.
---

# Meta violation-material cleanup

This skill maintains the independent job on CPU `43.166.187.96`. Source lives in
GitHub `gaoyuan12356416/ai-`, directory
`server-monitors/meta-violation-material-cleanup`. Read
[OPERATIONS.md](../../OPERATIONS.md) for commands, rollout and recovery.

## Fixed business contract

- Scope: DramaWave root product `1479`, HotDrama root product `3302`, and each
  root's direct `ads_apps_setting.ads_app_id` children. Do not expand by names,
  native app IDs, descendants of children, or account membership alone.
- Every run freezes the preceding `30 * 24` hours, using vio `created_at` in the
  half-open interval from window start through, but excluding, run cutoff.
  Refresh the complete window on both runs; late updates to old vio rows matter.
- Match only seven exact `issues_info.global` policy names: `Adult Nudity and
  Sexual Activity`, `Adult Sexual Solicitation and Sexually Explicit Language`,
  `Adult Sexual Exploitation`, `Human Exploitation`, `Violence & Graphic Content`,
  `Spam`, and `Drugs and Pharmaceuticals`. Generic rejection codes and
  Campaign/AdSet/account qualification issues do not establish these reasons.
- Count distinct violating `ad_id` per canonical material ID. Multiple policies
  on one ad count once. A multi-material violation counts only when its offending
  material is explicitly attributable; never distribute one uncertain event to
  every material.
- Candidate ads must be currently free of review issues according to Meta.
  Historical violations do not disqualify a currently recovered ad. Pending
  review, in-process, disapproved, deleted, archived, unreadable, contradictory,
  or unknown current state is skipped. Normal omission of empty feedback is not
  itself a failure.
- Rule 1: at least one qualifying violating ad for a material in the window,
  and the candidate ad or its AdSet or Campaign is paused.
- Rule 2: at least four distinct qualifying violating ads for a material in the
  window, regardless of whether the currently valid candidate ad is paused.
- A current ad containing a matched material can be deleted as a whole. Delete
  its frozen current Creative first, then that ad. **The operator explicitly
  permits deletion of shared Creatives.** Record outside references/impact, but
  do not protect or skip a Creative merely because it is shared. Do not add any
  nonqualifying ad to the deletion set. Do not delete Videos, images, Campaigns,
  AdSets, material-library records, or business-database rows.

## Runtime and inspection

- Runtime: `/mnt/data-disk/meta-violation-material-cleanup`; `current` points to
  a GitHub-sourced repository checkout under `releases/<sha>`.
- Config: runtime `config.json`; initial `live_enabled=false`. Keep credentials
  in the referenced existing env/Feishu files, never in config, logs or reports.
- Shared object ledger: `/mnt/data-disk/fb-ad-asset-delete/tasks.sqlite3`.
  Preserve its receipts, object claims and unknown fences across all tasks.
- Service/timer: `meta-violation-material-cleanup.service` and `.timer`;
  `09:00` and `21:00 Asia/Shanghai`. Timer has catch-up enabled. The internal
  run lock and legacy repair lock prevent overlapping execution.
- Skill installation is a symlink at
  `/root/.codex/skills/meta-violation-material-cleanup` into the current release.

Start with `status`, timer/service state, the run's artifacts and journal.
`preview` performs zero Meta/source-database mutations; it may save local audit
artifacts. All scheduled source reads use `63350` through `/usr/bin/mysql` FIFO
Gate with `SQL_GATE_BYPASS=0` and confirmed `@@read_only=1`. Never bypass the Gate
with a direct driver, `mysql.real`, or a write endpoint.

`graph_read_workers` controls only GET concurrency: integer 1–16, default 4.
DELETE and SQL remain serial. Inspect `graph_metrics` for requests, retries,
rate-limit responses and usage header maxima; zero header observations means
the platform supplied no usable measurement, not zero usage. Tune read workers
only against a complete read-only throughput run and current rate-limit evidence.
MySQL protocol compression is requested by default (`mysql_compress=true`),
with identical full-row hashes verified against the FIFO read endpoint. Keep
all pages at or below 2,000 rows and indexed ID batches at or below 1,000;
product/credential/lineage directories use keyset pagination, never truncation.

## Execution and recovery

The saved schedule operates within the user's authorized rules. Maintenance or
inspection alone does not start an extra deletion run. When a manual run is
already requested/authorized, use the existing frozen run and its durable
claims; do not request duplicate permission or create a replacement run to
evade locks.

Converting a preview or continuing a partial/interrupted run uses the original
manifest only when its hash is valid and its `frozen_at` is no more than 3600
seconds old. This is the scan-completion freeze time, not the initial run time.
An expired manifest requires a new complete scan and new run ID, preserving all
old receipts and unknown fences; never edit the timestamp/hash to extend it.
`--max-ads` selects complete Creative groups and may select zero ads when the
limit is smaller than every group. Continuation executes pending objects only.

Every platform write follows a committed shared-Store claim. Report API-accepted
deletions separately from exact-ID/account `DELETED` readback verification.
Timeout, interrupted submission and inconclusive readback stay unknown; never
rotate Tokens or replay an uncertain DELETE. `reconcile --run-id` is GET-only.
Missing objects or permission failures alone do not prove deletion. Preserve
successful work and explicitly report unscanned accounts and skipped objects.

Start and completion broadcasts go to 郜远 through the configured Feishu App.
If only the completion broadcast failed, use `notify --run-id ... --phase end`;
this must not trigger scanning or deletion. Inspect saved send receipts before
recovering an uncertain notification to avoid duplicate broadcasts.

Each live continuation has a separate notification phase: first `start`/`end`,
then `start-2`/`end-2`, and so on. Read `execution_attempt` and `notifications` in
`status` before recovery; `--phase end` does not select the latest attempt.
Use the exact saved phase and body. Failed notices may retry with the original
provider UUID; sending/unknown notices must not automatically replay.

Large broadcasts are one logical notice with durable child phases such as
`end-2-part-001`. Each complete serialized UTF-8 JSON request is at most 100,000
bytes; all reference information is retained across parts. Every child has a
stable UUID/body/receipt, and the parent becomes sent only after every child is
sent. Retry a known-failed parent phase to reuse its frozen plan and skip sent
children. Any sending/unknown child or parent requires receipt reconciliation,
never blind replay. Explicit HTTP 4xx rejections retain only numeric code/status;
ambiguous errors remain unknown.

`status` does not recover workers. Recovery requires the task process lock and
proof that the stored PID/start identity is dead. Scope shared-ledger recovery
to this task's `policy-cleanup-<run_id>` job, automatic actor, and matching shared
run; never invoke global Store recovery. A systemd start uses no run ID and
creates a new scan; it does not continue the previous manifest. The service has
an 11-hour runtime limit, SIGINT with a 180-second stop grace period, and no
automatic restart. Preserve interrupted results and check the manifest's
expiry before any explicit continuation. The old 10:00 cleanup has priority:
09:55–10:05 admits no new DELETE, releases the legacy lock, and waits to resume
the remaining work without repeating completed objects.

An interrupted run with `recovery_pending=true` still needs its scoped shared
fence confirmed. Keep it discoverable across restart until that acknowledgement;
the local interrupted status alone does not prove both ledgers were recovered.

Code changes are GitHub-first: test, commit/push, fetch the exact SHA on CPU,
deploy an immutable release, verify its hashes and run output. Restart only this
job when needed. Roll back code/config and preserve current ledgers and run
artifacts; never restore an old SQLite backup over deletion progress or restart
unrelated publishers.
