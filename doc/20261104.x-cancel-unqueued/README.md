# Cancel unmaterialized X targets

Operator requested cancellation of the 311 unmaterialized November 3 targets.
The operation preserves original account scopes, completed publications, pool
reservations and the 80 queues already created in partial batches. It does not
send requests to X, restart services, change capacity or affect future dates.

The script defaults to read-only preview. Apply requires the exact manifest SHA,
a private SQLite backup, actor and reason. Under BEGIN IMMEDIATE it rechecks the
same identities, existing queues and missing targets; concurrent materialization
causes an all-or-nothing abort. Empty failed batches become `cancelled`; partial
batches retain their state. A separate cancellation ledger records each missing
account/slot, and narrow SQLite guards prevent those slots being recreated,
including through the existing compensation-parent routes. All queue updates
that advance existing tasks remain allowed.

Expected scope: 1031=35, 1034=89, 1036=47, 1039=51, 1042=89. Do not expand to
other dates or cancel queued/active siblings to maintain a numerical target.

Validation: `python -m unittest discover -s tests -p test_x_cancel_unqueued_slots.py`.
Rehearse apply and idempotency on a private production DB backup before live
application. Verify all 311 cancellation rows and the 80 existing queue identities.
Compare preserved queues/logs to the rehearsal backup and verify ongoing service
PIDs remain unchanged. Historical sent report snapshots are immutable; this is
execution cancellation, not a revision or resend of the original report.

Rollback is data-scoped and requires a new operator instruction: read the batch
audit's previous_runs_json, ensure no target has since materialized, delete only
the manifest's cancellation rows, restore only the three still-cancelled empty
runs' status/error/finished/updated fields, then remove that batch audit record.
Do not restore the full backup or roll back publication/account databases. The
generic triggers can remain; with no corresponding cancellation rows they do
not affect any task. Removing the cancellation enables future recovery; it
does not itself authorize or execute a retry.
