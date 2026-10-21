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
