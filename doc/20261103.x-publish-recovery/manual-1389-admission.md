# Personal task 361 / manual run 1389

The original task failed before queue creation at `2026-11-03T10:53:21Z`:
`x_post_manual_preflight_failed`, `read-only MySQL connection failed: OperationalError`.
Its original task, batch 315, frozen account 43 / material 5188422 / Japanese
language, dates, copy, pool item 1008 and slot 836 remain authoritative.
The original reservation 1426 is released with `x_video_batch_preflight_failed`.
The original 10:48 authorization and all failure evidence are retained.

There is no existing personal retry API. Changing the released reservation back
to active violates its irreversible transition guard; the `(run, material)`
unique key also prevents adding another reservation for the same run.

This incident therefore uses one fixed admission, not a general retry policy:

1. The maintenance helper verifies exact original row hashes, zero queues and
   material occupancy, the original authorization, account availability and
   unknown-result fences. It backs up the native database and repeats those
   checks under `BEGIN IMMEDIATE` before recording an immutable admission and
   returning only the original run to `queued`.
2. The ordinary manual runner performs account, source, copy, language and media
   preflight. The normal sidecar still validates trusted account data.
3. In the existing queue-creation transaction, the narrowly scoped admission
   rechecks the original frozen rows, account blockers and all native material
   occupancy. Only one successful candidate and no partial failures may pass.
   The queue and immutable consumption receipt commit together. Reservation 1426
   remains released; the created queue becomes the durable execution record.

No task, batch, execution ID, date, copy or source snapshot is replaced. No X API
call is made by this helper. Reapplying the same admission does not reset a later
failed attempt. Existing queues return the normal idempotent plan response.
Other released reservations remain rejected. Any drift requires review, not a
new broad reset. In particular, 1390/1401 remain outside this scope.

Deploy `features/x_posts/service.py` and
`features/x_video_publish/recovery_1389.py` together during the planned idle
sidecar window, and verify the running files before admission. Do not rearm on
an old sidecar: it will correctly reject the released reservation again.

```sh
python -m scripts.recover_x_manual_1389 plan \
  --db /var/lib/x-post-automation/accounts.sqlite3 \
  --output PRIVATE_DIR/1389-plan.json

python -m scripts.recover_x_manual_1389 apply \
  --db /var/lib/x-post-automation/accounts.sqlite3 \
  --plan PRIVATE_DIR/1389-plan.json --plan-sha256 REVIEWED_PLAN_SHA \
  --backup-dir PRIVATE_DIR/backups --actor OPERATOR \
  --reason '2026-11-03 authorized zero-write recovery of original personal task 361' \
  --output PRIVATE_DIR/1389-apply.json
```

The plan binds helper/runtime source hashes. Plan and apply must use the same
deployed checkout. The grant tables are `x_video_1389_admission` and
`x_video_1389_admission_consumed`; the first stores complete original evidence,
the second stores the unique queue identity/hash. Both reject update/delete.

Validation: 78 offline tests across exact admission, personal core, manual
sidecar/runner and original manual recovery passed. Tests cover an intervening
reservation/queue, fresh account unknown, account/language/pool drift, wrong or
missing grants, subsequent failure preservation, both transaction rollback
boundaries, append-only history, unchanged irreversible reservation guard and
zero Post calls. No production apply is performed by this development step.
