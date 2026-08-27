# FB pending Page reset and Hong Kong GPU migration

## Scope

Replace the three unsubmitted automatic runs 29/30/31 on 2026-08-27
(Beijing 19:18, 20:59, 22:54). Preserve original run/task/Page/media identity,
record the old tasks as skipped, and create replacement automatic slots at the
same times. Read the current group 62 membership and require the approved ten
Page IDs on every planning read. Do not replay completed runs or Graph attempts.

Move only the prepare-only FB random-overlay worker from 43.166.178.132 to
43.154.250.89. CPU continues using loopback 18836, forwarding to HK loopback
8836. Keep the profile, deterministic recipe, asset manifest, COS prefix and
internal bearer unchanged. Existing X and drama synthesis services stay running.
The worker modules are byte-identical to the old GPU release after LF checkout.

## Procedure

1. SQLite online backup and timer/config snapshots. Stop plan/prepare timers;
   let the current preparation finish. Do not interrupt a publication.
2. `scripts/fb_auto_post_reset_pending.py` produces a read-only manifest. Apply
   requires its exact SHA256, operation ID and run IDs. It refuses past dates,
   changed templates, active leases, Graph IDs, unknown outcomes, any publication
   ledger or attempt, and snapshot drift. Preparation attempt_count is not a
   Graph attempt. The cancellation and replacement-slot insertion are atomic.
3. Rehearse on a SQLite copy using `plan_replacements`, the normal material
   repository/cooldown rules and `ExactPages`. Require three complete plans with
   ten distinct expected Pages and no skipped tasks before applying live.
4. Deploy this exact GitHub commit to HK, preserving the isolated Python venv.
   Use HK unit variants under the standard FB unit names. HK work root is
   `/var/lib/fb-page-random-overlay` on its 197 GB root volume; it has no `/data`
   mount. Transfer secrets only through authenticated SFTP; mode 0400.
5. Verify resource archive SHA256 and asset manifest; verify NVENC locally.
   Copy completed job manifests only after drain. Do not copy in-progress jobs.
6. Back up CPU authorized_keys and add only loopback 18836 to the already
   HK-source-restricted tunnel identity. Do not change existing forwards or SSH
   policy. Disable old FB tunnel, enable HK FB tunnel, verify CPU-to-HK health
   and authenticated manifest replay. Keep old worker as stopped fallback.
7. Apply reset and create replacement plans with ordinary create_run guards.
   Verify exact Page IDs and schedule, no old runnable tasks, and unchanged
   preexisting publication ledger/attempt tables. Restore all original timers.
8. Verify one legitimate newly prepared job on HK with matching CPU SHA/profile
   and no Graph test posts. Do not wait for all scheduled publications.

## Rollback

GPU: first stop the CPU prepare timer and drain the current prepare call.
Stop/disable HK `fb-page-random-overlay-tunnel.service`; enable/start old GPU
`fb-page-random-overlay-gpu.service` and then
`fb-page-random-overlay-tunnel.service`. Verify CPU loopback 18836 health and
restore CPU prepare timer. Preserve all completed manifests on both hosts;
copy HK completed manifests back for job replay if any retry needs them.

Queue: do not restore an old SQLite database over new publication evidence.
The replacement operations retain the original records and their exact time.
If operator rollback is needed, first stop planning/preparation, verify affected
tasks still have no publish attempts/ledger/Graph IDs or in-flight leases, then
cancel only the replacement tasks under an audited transaction. Re-enabling
old Page targets requires a new explicit operator decision; it is not automatic.

CPU API code and release symlink are unchanged. The reset script is an explicit
operator utility, not a newly exposed endpoint or a background reset policy.
