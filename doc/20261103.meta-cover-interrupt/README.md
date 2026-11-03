# Cover worker cooperative read interruption

The hourly lane rotation can stop a Graph read between two requests. The shared
Graph client raises `InterruptedError('stop_requested')`; the cover executor's
unexpected-error handler used to classify that as `worker_or_ledger_error`,
persist a global write pause and exit with status 78. Discovery continued while
the stopped worker accumulated pending tasks.

The cover-specific transport now translates only an active cooperative read
stop to the existing `RequestNotSent('cancelled_before_request')` signal, both
for ordinary reads and known-reference batches. Existing handling defers unsent
work, retains uncertain sent writes for read-only reconciliation, and postpones
incomplete cleanup reference checks. A shutdown during startup reconciliation
exits cleanly. Other interruptions, ledger errors and parameter mismatches retain
their existing failure behavior. No mutation is automatically replayed.

## Validation

Run from `server-monitors/meta-violation-material-cleanup`:

```sh
python3 -m unittest discover -s tests -p 'test_cover_interrupt.py' -v
python3 -m unittest discover -s tests
```

The regression uses real cover Graph cancellation and real SQLite guards to
cover hourly rotation followed by successful execution, interrupted CREATE/bind
readbacks without replay, cleanup inventory/identity stops without consuming a
DELETE attempt, shutdown during reconciliation, and unrelated interruptions.

## Deployment and recovery

Commit/push, fetch the exact SHA into a release, stop the cover scan/notify timers
and drain their services, then use `deploy/install_cover_worker.py prepare` and
`apply`. The candidate includes the already deployed card renderer and the same
four-hour notification schedule. Leave the main API and legacy deletion timers
unchanged. Keep current ledgers on deployment and rollback.

Before releasing a known interruption pause, preserve the exact pause JSON and
freeze the pending task IDs and counts in the deployment audit. Verify that the
failed worker is gone. A task incorrectly failed by this bug may be requeued
only with matching `worker_or_ledger_error` / `InterruptedError` evidence,
`request_sent=0`, no task write intent and no unknown shared reservation. Record
the previous result and an explicit recovery event; do not reset other failures
or any unknown write. Archive the verified pause instead of deleting its
evidence. Startup reconciliation must retain all unresolved sent outcomes.

Run a bounded natural cohort using `work --once --max-ads 256`, check receipts,
then start the continuous service and restore the scan timer. Track the frozen
cohort separately from new admissions, waiting reviews, failures and unknowns.
Do not report review-blocked or unknown tasks as successful replacements.

Rollback: stop the scan/notify timers, stop/drain the worker and any active
scan/notify service, then run the candidate release's
`deploy/install_cover_worker.py rollback <backup>`. It restores prior code and
units while retaining current ledgers and leaving the worker stopped. Preserve
the archived pause evidence and all new publication/deletion receipts.
