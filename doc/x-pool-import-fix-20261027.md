# Personal X pool partial admission repair

Confirming a preview that skips a source in the middle falsely returns
`pool_preview_changed`. The source selector assigns `manual_item_id` from the
position within each current request/page. Confirmation resolves only accepted
IDs, so later entries receive different positions even when every material is
unchanged.

The personal source broker now excludes only `manual_item_id` from its material
DTO. Every other source field remains intact. Pool consistency, account facts,
version, default copy, expiry, reservations and occupancy checks are unchanged.
There is no migration. Pre-deployment proofs require a fresh preview; historical
proofs, pool entries, reservations, queues and publication receipts are preserved.

Integration tests use the real SourceRepository, CachedSelector and PoolService
with a fixture MySQL adapter and isolated SQLite. They cover reservation skips,
duplicates, already-pooled and missing sources, a 50-source page boundary, fresh
preview after old proofs, idempotency, concurrent reservations, and rejection
of changes to actual identity, URL, content, duration, language or description.

Deploy from the exact pushed branch with `scripts/deploy_x_pool_import_fix.py`.
It verifies the live source baseline, backs up code/SQLite/tokens on the data
disk and updates only `features/x_video_publish/sources.py` in main and sidecar
trees. Only `drama-material-api.service` restarts: source resolution runs in its
broker. X SourceClient is unchanged and the X sidecar, Auto, pool scheduler and
material worker must retain their PIDs. No publishing timer, batch or queue is
stopped. Active publishers can legitimately advance during this deployment;
their ledger changes are never rolled back. A complete online SQLite backup
is retained as evidence only.

Health failure restores previous code and restarts only main. Rollback from
the same source checkout:

```sh
python3 scripts/deploy_x_pool_import_fix.py --rollback /absolute/backup/manifest.json
```

The repair does not import materials or send test Posts. Users refresh and run
the normal admission preview again. Skill context is unchanged; this repository
document records the new source DTO contract and operational procedure.
