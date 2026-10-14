# Material schedule snapshot consistency

A material schedule reads its candidate pool before metadata/media preflight.
Previously, atomic reservation compared the result against the newest live pool.
An administrator adding materials during that interval could invalidate the
whole prepared batch with `x_post_pool_fifo_conflict`, leaving no queue or Post.

The first scheduled pool read now freezes `material_pool_snapshot_max_id` in the
claimed run, in the same SQLite transaction as the pool read. Both candidate
selection and atomic FIFO validation apply that stored ID boundary. Later
additions enter the next batch. Reconnects, worker restarts and account-scope
rebuilds retain the boundary. It is never accepted from a submitted plan.

The authenticated internal pool request carries the full frozen schedule
identity. The store checks source/date/time/version/account order, `claimed`
status, no plan attempt and no queue before initializing or reusing the boundary.
Zero means an empty snapshot; NULL preserves legacy behavior for historical and
non-scheduled paths. The additive migration leaves historical values unchanged.
It does not reset any failed run, create a recovery task or publish a Post.

Within the boundary, newest-first ordering, current queue occupancy, active
manual reservations, account/language/relay guards, current validation proofs,
and unknown-outcome protections remain in force. Removing or consuming a chosen
material still fails safely. The ID filter is applied before LIMIT, so new rows
cannot displace older rows from the original scan capacity.

## Validation

Run the nine unittest modules in `install.py` (348 tests on the development
checkout), plus `git diff --check` and Python compilation. Coverage includes
150 new rows between selection and reservation, durable reuse after a new store
instance, the next batch seeing new rows, zero/NULL boundaries, invalid identity
and plan-attempt guards, in-bound FIFO bypass rejection, out-of-bound candidate
rejection, current occupancy and full client/sidecar/store transport.

## Deployment and rollback

Push the exact commit to GitHub, fetch it into the CPU deployment mirror, and
archive that commit under `/mnt/data-disk/x-post-automation/maintenance`.
Run `python3 SOURCE/ops/x-material-snapshot-20261014/install.py --commit COMMIT
--backup BACKUP` with a new backup directory under that maintenance root.

The installer checks the mounted data disk and exact live file hashes, stages
only four changed files onto the verified composite, and runs the Linux suite.
It then drains existing timers/workers, holds the publishing lock, stops
the X Sidecar, its dependent X Auto Sidecar and main API, and backs up both databases, token files and changed
main code. It rehearses the migration on a copy before applying it as the X
service user. Health, exact hashes, complete X Post table fingerprints, token
hashes/modes and SQLite quick_check must pass before restoring timer states.
The X Auto unit has `Requires=x-post-automation.service`, so it must be explicitly
restored before health acceptance. Its run/task fingerprints must also match.
A rescue timer and finally block restore
previously active services/timers after a failure.

For rollback run `python3 SOURCE/ops/x-material-snapshot-20261014/install.py
--rollback BACKUP`. This drains current workers, switches to the saved release,
restores the main service file and restarts the three dependent services. It
preserves current timer choices. Never restore databases, tokens or publishing
history; the nullable additive column may remain after a code rollback. Drain
an already preparing snapshot-bound batch before reverting to older code.

Production identifiers, backup receipts and real data remain private and are
not included in this repository.
