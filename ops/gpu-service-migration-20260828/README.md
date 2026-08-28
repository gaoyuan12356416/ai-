# GPU service migration — 2026-08-28

Run ID: `gpu-service-migration-20260828T1502`.

Approved destinations: US GPU 43.166.178.132 -> HK GPU 43.154.250.89 for
TT random/direct-outro, ad generation/vision, drama GPU API and X repair;
screenshots/covers -> CPU 43.166.187.96. Keep Kronos and HK FB unchanged.

HK `/data` is explicitly permitted to use its root volume. At execution start
the volume had been expanded to ~504 GiB, with ~428 GiB available. CPU must use
the separately mounted `/mnt/data-disk`, UUID
`3e8ac4e8-7770-456d-9e89-2ec5dd405fa8`. No cloud disk purchase or formatting.

## Ownership

- `control/`: coordinator; maintenance gates, source fencing and reconciliation.
- `cpu/`: CPU screenshot/cover runtime, storage staging and checks.
- `hk/`: HK ad runtime and X storage/runtime migration.
- `tt/`: isolated TT runtime, state transfer, offline tests and lane checks.

Only the coordinator authorizes production cutover, publishes Git commits,
changes CPU maintenance gates and fences US services. Preparation may create
isolated data directories and immutable backups, but cannot claim work, open
publish gates, stop active jobs or take over a production port.

## Evidence and backups

CPU: `/mnt/data-disk/migrations/gpu-service-migration-20260828T1502/`.
HK/US: `/data/migrations/gpu-service-migration-20260828T1502/` on each host.
Private configuration snapshots are server-only, directory 0700/file 0600.
Never commit environments, auth files, keys, SQLite databases or tokens.

## Invariants

1. Close new admission and pause triggers; let existing requests finish.
2. Confirm CPU claims/leases and source requests/children/locks are drained.
3. Stop, disable and persistently mask old business services and tunnels.
4. Freeze and verify final data before exposing the destination.
5. Restore only originally active triggers; never run a publish canary,
   `run-now`, `execute-next` or missed-slot catch-up for validation.
6. Preserve CPU databases, code routes, frozen IDs, profiles and COS URLs.
7. A post-cutover rollback first fences the destination and returns current
   TT manifests/publish facts; it must not restore stale publishing databases.

The shared screenshot/cover/ad/vision tunnel requires a joint short window.
HK drama continues on CPU 18788; X remains 18820. CPU local workers take
18790/18795/18798, HK ad tunnels take 18796/18797, HK TT takes 18830/18834.
Unused US 18792-18794 workers are archived and retired, not duplicated.

## Acceptance

Check private and public file URLs, image sizes, existing successful retry
outputs, CPU mount guards, HK free-space floor (30 GiB), data paths and tmp
namespaces, all port owners, old source masks, preserved schedules and
publish lineage. Synthetic/local media and mock upstream APIs only for post
workflow tests. Keep the final test report and rollback manifest for human
review; do not mark operator acceptance on their behalf.
