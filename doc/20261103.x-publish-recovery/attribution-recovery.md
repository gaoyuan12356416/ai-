# Zero-write attribution recovery, 2026-11-03

`scripts/recover_x_attribution_failures.py` admits only queues 5399, 5407,
5486, 5544, 5550 and 5552 from the authorized November 2–3 incident. These
failed in native `_links()` before a publish claim, link freeze or X operation.
The script never updates the native database, changes attempts, creates links,
renders media, or calls X. It only changes the verified central publish job
from `failed` to `ready`; the next normal claim increments its generation.

The original central error, preparation result, native source, account, recipe,
queue, log and history remain intact. The prior execution and evidence hashes
are recorded in an `audited_zero_write_attribution_recovery` event. Repeating
the same applied plan is a no-op. A fresh backup precedes a transactional CAS.

## Evidence and root cause

Read-only production validation at 2026-11-03 10:44 UTC passed all six plans.
Every internal resolver request returned HTTP 200; the response passed
`validate_context` and `build_url`, including the original queue identity and
log creation timestamp. The resolver endpoint itself performs read-only SQLite
and mapping queries. `freeze()` and short-redirect creation occur later in
native `_links()` and are not called by the diagnostic utility.

Historical API journal records show HTTP 503 in the exact failure windows of
5399, 5407, 5486, 5544 and 5552. The older caller collapsed HTTP errors into
`x_attribution_resolution_unavailable`, so their underlying response error was
not retained. For 5550, the job failed at 09:35:55.360 UTC between the main API
stopping at 09:35:55.274 and restarting at 09:35:55.380. An interrupted resolver
connection is the supported explanation, not proof of a missing mapping.

Current probes verify that this transient failure is no longer present. They
do not guarantee future availability. A new failed probe blocks admission and
records only safe HTTP status/error codes and the validated context hash.

## Operator sequence

Run the reviewed GitHub source on the CPU host; use new private filenames for
each step. Coordinate with deployment so normal claims remain paused during
the final evidence collection and central admission. Do not include unknown
or account-locked queues in this tool's scope.

1. `plan --central /mnt/data-disk/pipeline-dispatch/dispatch.sqlite3
   --native /var/lib/x-post-automation/accounts.sqlite3
   --queue-ids 5399 5407 5486 5544 5550 5552 --output PLAN`
2. Run `node-evidence --plan PLAN --node-id NODE --outbox OUTBOX --output FILE`
   on every node listed in the plan. The CPU outbox is
   `/mnt/data-disk/pipeline-node-cpu/pipeline-dispatch/receipts.sqlite3`; the GPU
   outbox is `/data/pipeline-dispatch/receipts.sqlite3`. Transfer only these
   private evidence files back to the operator host.
3. `resolve-evidence --plan PLAN --token-pid PID --output RESOLUTION`, where PID
   is the existing `x-post-automation.service` MainPID. The internal token stays
   in memory. It must never be passed on the command line or printed.
4. `apply --plan PLAN --node-evidence CPU_EVIDENCE GPU_EVIDENCE
   --resolution RESOLUTION --backup-dir BACKUPS --actor ACTOR --output RESULT`.
   All evidence must be at most 120 seconds old. Re-collect stale evidence.

Every invocation uses `python3 -B scripts/recover_x_attribution_failures.py`.
Plan and evidence actions are read-only. Apply also rechecks all current native
and central evidence: an attempt, success, unknown, operation, pending outbox,
live lease, hold, changed source/account/artifact, or later checkpoint rejects
the operation. The final platform credential and publishability checks remain
in the normal worker path.

## Validation

`python -m unittest scripts.test_x_attribution_failure_recovery
scripts.test_x_prepost_recovery` passes 16 offline integration tests. They cover
native database byte-for-byte logical preservation, one audited admission,
normal generation 2 claim, original artifact reuse, scope limits, source drift,
pending/unknown/historical successful receipts, resolver identity and freshness,
and central renderer receipt fields without relaxing artifact identity.

No production apply was performed during this script's development.
