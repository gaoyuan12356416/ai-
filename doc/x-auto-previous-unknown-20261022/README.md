# X Auto: historical account hold during plan creation

A plan transaction can reject an account with `x_post_unknown_outcome` (409,
`unknown_outcome=true`) because an older Post is unresolved. The executor used
to query its own empty canonical run, then replace that cause with a 503
`x_auto_execution_plan_missing`. Preparation retried indefinitely.

After an identity-checked canonical readback proves this task has no queue,
preserve this explicit 409 as `x_post_account_needs_review`. The existing failure
path records a terminal preflight failure and releases only the provisional
material reservation. The older unknown Post and its account hold remain intact.
An existing canonical queue still wins over the error response. Transport-unknown
failures keep their existing retry/readback behavior.

Validation:

    python -m unittest scripts.test_x_auto_post_publisher scripts.test_x_auto_post_store scripts.test_x_auto_post_x_sidecar scripts.test_x_auto_post_service -q

## Scoped deployment

The source baseline is a2e08daf7d1978db834c24aff885e7d42bbab075. Verify the live
publisher SHA256 is fdc481f69f7340bb32a494956d5071c92627fb0639c4546c95156ec169dd8b55
before overlaying this change. Fetch the new commit from GitHub into a separate
checkout. Copy the complete current runtime into a new immutable release and
overlay only `features/x_auto_posts/publisher.py` and its test file. Run the
regression suite in that assembled release.

Save Auto trigger states, stop only its active runner/path, scheduler and metric
triggers, and let their workers and HTTP requests drain. Do not interrupt an
active publish. Snapshot the Auto database and save release/trigger state in a
private backup manifest. Switch `/opt/x-post-automation/current` atomically and
restart only `x-auto-post-service.service`. Verify port 18833 `/health` and restore
the exact saved trigger states. No account, template, queue or token migration.

Rollback: pause/drain the same Auto triggers, atomically restore the prior release
symlink from the manifest, restart only the Auto service, verify health and restore
trigger states. Never restore an older database or token backup over live data.

## Operational reconciliation

Before releasing a historical account hold, obtain the exact Post through X's
read API and match author, expanded text, media key and creation time against the
frozen log. Use `scripts.x_post_gap_recovery.reconcile_direct_post` first in dry-run
mode, then after an online database backup. Confirmed direct-Post reconciliation
holds a SQLite write transaction and rejects any active write on the same account;
it does not require an unrelated batch to release the global runner lock. The
original failed/unknown state and exact platform evidence are validated inside
that transaction before updating anything. All rearm/retry operations still
require the global runner lock. The confirmed reconciliation
retains an audit of the original queue/log, attaches the confirmed Post ID and
updates the pool/run through existing aggregation. Never retry that old Post.
Keep account-specific evidence, backups and receipts outside Git.

Already pending zero-attempt tasks may continue through their existing runner
after successful reconciliation; do not reset their material/run identity or
replay terminal historical tasks. Verify a new successful Post and its platform
readback before claiming restored delivery.
