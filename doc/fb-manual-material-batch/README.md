# Operator-selected FB material round

The root-only CLI `scripts/fb_auto_post_manual_material_batch.py` previews and
reserves one manual run. Each eligible Page receives one material in its own
language. Pages are sorted numerically; materials rotate in input order. The
send order interleaves en, es, zh-tw, id and th, omitting exhausted languages.

Use `--preview --template-id ID --material-ids ID ... --operation-id UNIQUE
--waive-series-blacklist SERIES --waive-drama-cooldown --output MANIFEST` only
when the operator has explicitly authorized those batch-specific exceptions.
Then `--apply MANIFEST` performs source/hold/capacity revalidation and one atomic
reservation. Repeating that exact apply returns the existing run. An operation
ID with a different manifest is rejected. No public API or schema is added.

`--status OPERATION_ID --output REPORT` exports task state and request order.
`--verify OPERATION_ID --output REPORT` additionally checks Meta published state
and obtains the actual permalink without exposing Page credentials. A ledger
video ID alone does not prove a Post ID or publication.

All source reads use 63350 and verify `@@read_only=1`. This manually invoked CLI
uses the reserved operator connection pool, one connection at a time. It must
not be repurposed as a scheduled source collector. It reads the running sidecar
environment in process memory. Runtime manifests/reports/backups belong on the
mounted data disk; never commit secrets or runtime manifests to Git.

Before apply, back up the live SQLite database using the SQLite backup API.
Deploy only `core.py`, `gpu.py`, `manual_batch.py` and the CLI from the verified GitHub
commit, after comparing the live core baseline. Keep other deployed modules,
template settings and timers unchanged. Drain leases before the narrow sidecar
restart. The manifest hash, exact task identity and ordered task set are checked
again at claim; later tasks cannot claim while an earlier batch task is still
planned, preparing, ready or running. An accepted submission allows the next
Page to proceed while reconciliation confirms publication.

Ordinary runs retain their existing drama cooldown and selection blacklist.
All batches retain same-material reservations and unknown-result Page holds.
The root-only entry point is the trust boundary for approved exceptions; a
client cannot request these flags through the existing run-now API.

Manual COS sources saved as HTTP are verified over HTTPS on the configured
source host before reservation. The immutable original URL is retained in the
receipt; only a validated manual task uses the deterministic HTTPS transport
URL. Existing automatic source handling and the GPU host allowlist stay intact.

`--recover-unattempted-preparation OPERATION_ID --output AUDIT` permits an
in-place retry of this specific pre-upload URL validation failure. It verifies
HTTPS size/ETag against the original receipt, checks exact task identity and
requires no Graph attempt, ledger, unknown flag or Post ID. It retains original
task/job IDs and saves the prior failure state rather than recreating the batch.

Rollback restores only the changed code and cancels only batch tasks with no
Graph attempt. Preserve the current SQLite, submissions, attempts, wrappers
and frozen recipes. Never restore the old database over new publishing facts.

## Group 62 production operation, 2026-10-08

- Source release: `b98bbdb44af9ee4de09b19540e488fc028dbdad4`, branch
  `codex/fb-page62-manual-round-20261008`, PR 9 in `gaoyuan12356416/ai-`.
- CPU host: `43.166.187.96`, runtime `/opt/fb-auto-post/current`. Preserve the
  surrounding incremental release; copy only the four files named above from
  the verified GitHub checkout. Heavy rendering stays on the existing Hong Kong
  GPU service and its existing concurrency/host allowlist.
- Operation `group62-round-20261008`, run 242, task IDs 18932–19038, 107 unique
  Pages. Original receipt SHA-256:
  `38750e27eeb0e45e924e69e4e9e5301485432f4612be4c897dff812fb8143681`.
- Private operation files:
  `/mnt/data-disk/fb-manual-material-batch/operations/group62-round-20261008`.
  Keep manifests, recovery audit and reports private; no Page tokens are stored
  in these files.
- Initial backup: `/mnt/data-disk/fb-manual-material-batch/backups/20261008T104954Z`.
  Follow-up source/SQLite backup:
  `/mnt/data-disk/fb-manual-material-batch/backups/20261008T110459Z`.
  Deployment receipts include changed-file hashes, narrow health readback and
  unchanged template/version/publish-ledger/attempt checksums.
- Tests: `python -m unittest scripts.test_fb_manual_material_batch
  scripts.test_fb_auto_store scripts.test_fb_auto_publisher scripts.test_fb_auto_v2`:
  105 passing on Windows and CPU Linux after the reporting follow-up.
- First preparation rejected the catalog HTTP URI before any GPU/Meta upload.
  All 107 original tasks were restored in place after HTTPS size/ETag validation
  with zero Graph-attempt/ledger rows. Recovery audit:
  `preparation-recovery-https-v2.json`. No task, Page allocation, source identity,
  job ID or receipt hash was replaced.
- `attempt_count` counts both preparation and publishing claims. Use the
  `fb_auto_publish_attempt` table to prove publication attempts and their order;
  task `started_at_utc` begins at preparation and cannot establish send order.
- Keep active blacklist 170 for drama 25639 and automatic template 1/version 4
  intact. Only the root-created, exact-receipt manual run gets the approved
  exception. Five blocked Pages and seven unmatched ko/ja/de materials remain
  outside the run.

Read status or confirm actual publication without modifying tasks:

```bash
cd /opt/fb-auto-post/current
python3 scripts/fb_auto_post_manual_material_batch.py --verify group62-round-20261008 \
  --output /mnt/data-disk/fb-manual-material-batch/operations/group62-round-20261008/verified.json
```

Rollback procedure: pause only this operation's tasks that have no Graph attempt,
save a fresh SQLite backup, then restore `core.py` from the initial backup and
`gpu.py` from the follow-up backup. The new `manual_batch.py` and operator CLI may
be left dormant or moved aside after checking that no other manual run needs
them. Restart only `fb-auto-post-service.service` after draining in-flight
sidecar requests, and restore the timers to their previous states. Retain the
current SQLite, accepted/published/unknown tasks, Graph attempts, public wrappers
and GPU recipes. Never restore an old SQLite snapshot over new facts.

The exact run-scoped cancellation and code restore commands are in
[`rollback.md`](rollback.md).
