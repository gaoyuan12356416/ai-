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

## Execution and recovery

The saved schedule operates within the user's authorized rules. Maintenance or
inspection alone does not start an extra deletion run. When a manual run is
already requested/authorized, use the existing frozen run and its durable
claims; do not request duplicate permission or create a replacement run to
evade locks.

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

Code changes are GitHub-first: test, commit/push, fetch the exact SHA on CPU,
deploy an immutable release, verify its hashes and run output. Restart only this
job when needed. Roll back code/config and preserve current ledgers and run
artifacts; never restore an old SQLite backup over deletion progress or restart
unrelated publishers.
