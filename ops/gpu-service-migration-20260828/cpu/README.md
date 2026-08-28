# CPU screenshot and cover migration

Owner: CPU migration worker. This package never edits `app.py`, Nginx, crontab,
existing CPU workers, the US services, or their tunnels. The root coordinator
owns admission fencing and the one shared tunnel cutover window.

## Fixed interfaces and storage

- Preserve existing CPU `8790/8795/8798`; new isolated units take over
  `127.0.0.1:18790/18795/18798` only after US service shutdown and port release.
- Keep screenshot v7, concurrency 1 per sidecar and per dimension, source checks,
  cache semantics, and the existing four-endpoint API pool. No business posts or
  production job creation are part of migration validation.
- Frozen `runtime/` files were downloaded byte-for-byte from the deployed US
  service. `source-manifest.json` records their original hashes; do not format
  or otherwise change them.
- New runtime, generated images, caches, job workspaces, CLI state and logs live
  below `/mnt/data-disk/codex-workers/us-migrated`. The CPU disk must have UUID
  `3e8ac4e8-7770-456d-9e89-2ec5dd405fa8`; an absent/wrong/read-only mount fails closed.
- Existing literal public paths are preserved by atomic directory-to-symlink
  exchanges. Do not change only `PUBLIC_ROOT`: callers supply absolute output
  paths, and mismatched lexical paths produce invalid `../` file URLs.
- CPU authorization remains CPU-owned. Screenshots keep the current root source
  and short synchronization lock. Cover's thin entrypoint places its CLI state
  on the data disk and synchronizes the same authorization under that lock.
  The lock is not a cross-host lease; source shutdown remains mandatory.

## Execution phases

Run as root on CPU from the exact clean GitHub checkout of this package. Set
`RUN=gpu-service-migration-20260828T1502` for this operation.

1. `python3 migrate_cpu.py backup --run-id "$RUN"` creates a private configuration
   archive and online SQLite backup, with `quick_check`, hashes and an audit.
   It does not replace an existing original backup.
2. `python3 migrate_cpu.py precopy --run-id "$RUN"` copies CPU public and work
   directories to the data disk. It preserves source paths and processes and
   writes file/hash manifests. A successful preliminary copy is not final sync.
3. After the coordinator supplies a pushed commit and install authorization,
   `python3 migrate_cpu.py install --run-id "$RUN" --expected-commit "$COMMIT"
   --authorization "$INSTALL_JSON"` verifies a clean exact checkout and frozen
   hashes, installs new units and log rotation, and leaves every new unit stopped.
   The install JSON must contain the exact run id and
   `"install_authorized_by_parent": true`.
4. The coordinator fences public module writes, the direct screenshot cron and
   both test services; waits for ad/screenshot work and all drama work to finish
   before moving shared directories; then supplies fresh maintenance evidence.
   The fence must also block **all methods on the exact path**
   `/api/drama-screenshot-material/jobs/batch`: the live application's GET
   handler accepts a JSON body and creates screenshot batches. Normal GET
   `/jobs` and `/jobs/{id}` must remain readable. Wait for the drama worker
   lease to be released, not only for the job row to become terminal.
   Run `python3 migrate_cpu.py cutover-storage --run-id "$RUN" --authorization
   "$MAINTENANCE_JSON"`. Atomic `RENAME_EXCHANGE` preserves the public lookup
   path, retaining the original directory as `.pre-$RUN` for rollback.
5. Only after the coordinator stops and disables/masks all six US image service
   units and both related tunnels, run `python3 migrate_cpu.py start --run-id
   "$RUN" --authorization "$SOURCE_STOP_JSON"`. It verifies all fences,
   freshly stopped source evidence, media placement and free ports. The root
   coordinator separately transfers `18796/18797` to HK; `18788` is untouched.

Maintenance evidence is a private JSON file, valid for five minutes. Required
fields: exact `run_id`, `authorized_by_parent=true`, `observed_at_epoch`,
`writes_fenced=true`, `cron_fenced=true`, `test_api_fenced=true`. Start additionally
requires `source_tunnels_stopped=true` and `source_units` keyed by all six names
in `SOURCE_UNITS`; each must report `active=inactive`, `enabled=disabled|masked`,
and `children=0`. The coordinator must populate these from fresh live evidence,
not assumptions. No password or token belongs in these files.

US job/cache/public data must be copied to a private data-disk import directory
before complete source-host retirement. Exclude `.codex`, `codex_home` and `auth.json`; never
replace CPU authorization or its live SQLite. Preserve differing source artifacts
in the private archive rather than overwriting CPU's published content. Initial
data copies must be repeated/checked after source quiescence. Do not delete source
history or root rollback directories as part of this package.

For this run, the coordinator explicitly retained the US host and its verified
archive on `/data/migrations/$RUN/cpu`. Full cold-history transfer was paused
because US outbound bandwidth is shared with required HK deployment resources.
This does not authorize deletion of the archive or US history. Copy only missing
public images into inactive CPU staging; preserve existing CPU files on conflicts.
The three missing old MP4s (2.260 GB) are not the current CPU public artifacts:
the two corresponding live job URLs use COS and returned HTTP 200, with different
byte sizes; the third has no current CPU job row. They remain in the US archive.

The live GPU render and cover calls are synchronous requests to
`18788/api/gpu-video/render` and `/api/gpu-video/cover`. The GPU reads
`GET /api/gpu-video/random-overlay/catalog` on CPU. None is under the three
material module prefixes, and the screenshot completion callback is outbound
from CPU. Keep these GPU paths outside material admission fences. The normal
new/retry/review endpoints for the three affected modules are under their
expected module prefixes; the screenshot GET batch route is the extra admission
path described above.

## Verification and rollback

- Local: `python -m unittest discover -s tests -v`; syntax-check all Python
  files. The tests exercise wrong mounts, escaped paths, changed runtime hashes,
  source-active/child/stale/fence conditions, and occupied-port protection.
- Before starting: `systemd-analyze verify` rendered units; validate mount guard,
  file hashes, fresh inactive source state and preserved existing CPU unit PIDs.
- After starting: health on the three new and three existing endpoints; `ss`
  proves the new ports belong to CPU workers, not SSH. Compare actual public
  GET body hashes and `/files/` body hashes for existing artifacts. Check final
  file manifests, Nginx access, read-only queues and all path `realpath/findmnt`.
  `python3 migrate_cpu.py verify --run-id "$RUN"` checks local port ownership,
  new worker health, unchanged existing-worker PIDs and public/files body hashes.
- Functional image canaries, if run, use an isolated data-disk directory and no
  production queue/database/callback; no real TikTok/X posts. Include three
  dimensions, cover generation, cache hit, and failed-dimension retry semantics.
- Code/endpoint rollback: re-fence writes, drain, run the `stop` phase with fresh
  maintenance authorization, prove all new units inactive, then let the root
  coordinator restore the US services/tunnels. Never start US before CPU stops.
- Prefer leaving data-disk symlinks in place during code rollback. The optional
  `rollback-storage` phase only allows an unchanged pre-cutover manifest and
  requires all drama/image work drained. It refuses rollback if new data exists.
- All evidence is under `/mnt/data-disk/migrations/$RUN/cpu`; configuration
  archives and databases remain private to root. Do not commit them.
