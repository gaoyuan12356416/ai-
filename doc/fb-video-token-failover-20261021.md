# FB video publish token failover

When a video publication receives an explicit Graph rejection (including code
368), try the next unused eligible token for that same Page. The old new-module
worker only rotated codes 10/102/190/200 and truncated candidates to eight.

- Read current eligible credentials at execution time through the existing
  repository, which excludes banned/empty credentials and deduplicates tokens.
- Try the finite candidate list until a receipt, an unknown result, or exhaustion.
  Each write has a durable attempt. All-rejected errors retain the last platform
  error code and display the number of attempted authorizations. This does not
  claim that every rejection is caused by missing permissions.
- Renew the existing execution lease atomically with each new attempt; never
  resurrect an expired claim or begin another write with an unresolved intent.
- A returned object ID always enters submitted/reconciliation, never a second
  publish. Timeout, disconnect, unparseable or unstructured responses, 5xx,
  redirects and missing success IDs remain unknown and stop rotation. Shared
  GraphTransport now requires a structured 4xx Graph error for definite failure.
- Existing reconcile limits and schedules remain unchanged. Loopback client
  timeout does not cancel the sidecar handler; durable running leases prevent
  another execution from claiming its task while the finite rotation completes.
- Historical failed tasks are not requeued, and no real test post is authorized
  by this code change. Existing records, links and publisher facts remain intact.

## Validation and delivery

Run `python -m unittest scripts.test_fb_auto_publisher -q`,
`python scripts/test_fb_video_publish_core.py -q`, and the existing pool/worker
regressions. Tests use temporary SQLite and fake transports only. Include code
368 -> success/unknown, all rejected with saved attempts, twelve slow tokens
exceeding the original lease window, receipt-with-error and interrupted claims.

Commit and push branch `codex/fb-video-token-failover-20261021`, then fetch that
exact commit on CPU 43.166.187.96. Run
`python3 scripts/deploy_fb_video_token_failover.py` before `--apply`.
The installer checks baseline d57a4d6, backs up five exact file copies and the
operational SQLite, drains producers, restarts only the shared FB sidecar, then
restores saved producer/timer states. No main API restart or DB migration.

Runtime files: `features/fb_video_publish/{core,worker}.py` in both
`/opt/fb-auto-post/current` and `/root/drama_material_service`; shared
`features/fb_auto_posts/publisher.py` only in the FB runtime. The main API's
unrelated publisher copy is deliberately excluded.

Rollback with the same release script and `--rollback <actual manifest.json>`.
This hash-checks and restores only code, preserves current DB and short links,
drains work and restores original unit states. Do not restore the DB backup.
Record exact release, backup path and production readback below after deployment.

Local checks: 181 passed, one optional live-snapshot test skipped. CPU's older
SQLite cannot construct one legacy migration fixture with DROP COLUMN; that
fixture is explicitly version-gated (it passes locally). This patch does not
change the schema or require SQLite DROP COLUMN in production.

## Production verification, 2026-10-21 17:26 Beijing

- Implementation `f5dfce95babe0ae149056ec77eb0b07b634fbc4c`; deployed GitHub
  release `c926ef6cdd452f9798d4c2c71e32b2204d45a038` at
  `/mnt/data-disk/fb-video-publish/releases/c926ef6` on CPU 43.166.187.96.
- Five installed SHA256 values match the release. FB sidecar restarted and its
  saved worker/scheduler/timer states were restored; all affected services are
  active with zero automatic restarts. Main API PID remained 2136222.
- All 11 module tables (35 rows) match the online pre-deploy backup, including
  the existing failed task and its one attempt. Four short-link hashes match
  their ledger; public failed-task link also matches. SQLite quick_check=ok.
- CPU release checks: 106 passed, one unsupported legacy fixture skipped.
  Tests importing the actual installed runtime: 49 worker/core and 14 Graph
  transport tests passed. Public records page 200, unauthenticated tasks API
  401, sidecar health ok; no post-deploy worker/traceback error markers.
- No real Facebook test posts, historical replay, database migration or change
  to existing schedules. Skill reference updated.
- Durable evidence: `/mnt/data-disk/fb-video-publish/evidence/token-failover-20261021.json`.

Exact code-only rollback (run on CPU, preserve DB and short links):

```bash
python3 /mnt/data-disk/fb-video-publish/releases/c926ef6/scripts/deploy_fb_video_token_failover.py --rollback /mnt/data-disk/fb-video-publish/backups/page-list-20261021T092612Z-c926ef6c/manifest.json
```
