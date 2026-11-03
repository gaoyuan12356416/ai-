# Unstarted manual backlog recovery

The pipeline cutover deliberately excludes manual requests created before its
boundary. Old zero-queue manual requests therefore remain queued even when the
manual timer and current publication workers are healthy.

The fix adds an exact, identity-bound `x_post_manual_resume_audit` permission.
The canonical claim can bypass only the cutoff for an approved ID. It retains
the original request timestamp, account/material order, templates, reservations,
future scheduling check, existing queue exclusion, and sibling unknown-write
fences. Existing databases without the audit table retain their behavior.

`scripts/recover_x_unstarted_manual.py` defaults to no action unless `plan` or
`apply` is selected explicitly. `plan` requires exact run IDs and their approved
Beijing dates; it uses read-only SQLite and checks the complete unstarted run
and active reservation evidence. `apply` validates the plan hash and current
rows, creates an online SQLite backup, rechecks under `BEGIN IMMEDIATE`, and
writes only the authorization audit. It never creates a queue or calls X.

Example (replace placeholders using the privately reviewed plan):

```
python3 scripts/recover_x_unstarted_manual.py plan --db <native-db> --run-ids <ids> --dates <dates> --output <private-plan.json>
python3 scripts/recover_x_unstarted_manual.py apply --db <native-db> --plan <private-plan.json> --plan-sha256 <sha> --backup-dir <data-disk-backup> --actor <operator> --reason <reason> --output <private-receipt.json>
```

Deploy the claim code and recovery module before applying. Repeated application
of the identical committed plan returns `already_applied` even if normal workers
have since progressed. Reverting code preserves audit records and publication
history; never restore the whole live database. Before revoking an unused
authorization, confirm the exact run still has no queue or publish attempt.

Validation: `python -m unittest scripts.test_x_unstarted_manual_recovery
scripts.test_pipeline_x_planning` passed 17 tests. Coverage includes precise
cutoff bypass, no queue creation, preserved frozen identity, idempotent apply,
unknown/started/out-of-scope rejection, state drift, invalid reservation, and
compatibility with a database lacking recovery records.
