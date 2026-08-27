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
   If HK already has the complete identical asset set, verify all manifest/file
   fingerprints and copy locally into the isolated FB directory instead.
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

## Verified production outcome (2026-08-27)

- Code release: `59b42e24cd1e57b1209cb7addde3de7a8c98568b`, fetched from GitHub
  on CPU; the same checked archive deployed to HK. Worker module SHA256
  `87fc6a373a1ba6208c6bcfc745b62fa8f703a1a7be9577c3108af92d89a34f5d`
  is identical to the old GPU worker.
- Runs 29/30/31: all 36 old-Page tasks skipped, identities retained. Replacement
  runs 37/38/39: ten current group-62 Pages each, no skips, three distinct
  materials per Page. Original publication attempts/ledger and all unrelated
  existing tasks/runs/Page snapshots have identical before/after fingerprints.
- New Page credential identity GET /me: 10/10 returned the expected Page ID.
  This is read authorization proof, not publication completion proof.
- Full FB tests 149/149 locally; HK worker tests 15/15; HK NVENC smoke passed.
- The existing HK asset set at
  `/data/drama-synthesis-gpu/assets/fb-v3-028326ab2114` passed full manifest/file
  validation and was copied into the isolated FB root. Source stayed unchanged.
  Asset manifest SHA256:
  `028326ab211418934b026c227f2e3707553cce7560551dca3c0bfddc681d566f`.
- 284 completed manifests migrated. CPU authenticated prepare replay for task
  388 returned exactly the original URL, 57,315,873 bytes and SHA256
  `ad99c070a96c833601952aec2de9abeb3eb761d249f0026a289673d5627d191e`.
- CPU 18836 sshd peer is HK `43.154.250.89`. Both HK FB units active/enabled;
  both old FB units inactive/disabled. CPU plan/prepare/publish timers active.
  Existing HK X and drama synthesis services/tunnels remain active.
- Real replacement task 437 (run 37) is preparing on HK, job
  `fb-page-156fefb0ce2b0dd3aaeba89788850a388545ef07c462e48c`; its ffmpeg
  process uses `h264_nvenc` and the isolated FB work root. No test posts made.
  The future scheduled publications have not been claimed complete.
- CPU audit root:
  `/mnt/data-disk/fb-auto-post-publisher/recovery/20260827-new-pages-hk`;
  old GPU backup:
  `/data/fb-page-random-overlay/backups/hk-cutover-20260827`.
