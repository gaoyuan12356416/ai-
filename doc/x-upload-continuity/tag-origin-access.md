# Tag recovery stopped before native admission

The Tag bridge now checks the immutable Auto task before reservation. Production
runs the publisher as `x-post-automation`, while its Auto database directory is
owned by `x-post-daily` with mode 0700. Root-only diagnostics concealed this:
`hit_timing.assert_due` could not open the read-only origin under the actual
publisher UID. It returned the generic `x_tag_dispatch_failed` before native
claim creation. Existing manual tasks bypass this Auto-origin read.

The Auto database also uses WAL: directory traversal alone is insufficient
when SQLite needs shared-memory sidecars. The tested ACL approach was reverted.
The publisher now uses an authenticated `/internal/x-tag/hit-origin` endpoint
on the existing internal Tag gateway. It executes fixed read-only SQL as the
Auto database owner with a minimal environment. No database or directory access
is granted to the publisher. Frozen task/account/material/time checks are
unchanged. Errors retain their codes; Tag bridge logs include only exception
types and frame locations, never values or source lines.

From the verified pushed checkout, run
`scripts/deploy_x_tag_origin_broker.py prepare --directory <deployment>`, then
`activate --directory <deployment>`. Only four files are overlaid on the live
combination; concurrent release hashes are checked. It pauses the two local
Tag intake triggers, fences new Tag claims, lets all active Tag jobs finish,
switches Tag publisher and gateway, then restores triggers after readiness.
Main X, coordinator, API and other channels continue. Rollback is
`python3 <same-checkout>/scripts/deploy_x_tag_origin_broker.py rollback --directory <deployment>`;
it follows the same safe drain. Never restore an old database over live facts.

For explicitly authorized recovered jobs that failed before native admission,
`scripts/resume_x_unentered_recovery.py plan --scope <private-scope> --output <plan>`
requires terminal failed jobs, no current-generation intents, operations,
checkpoints, native claims, receipts, publications or resource fences, plus the
still-armed one-generation permission. Review the private plan, then use
`apply --plan <plan> --backup-dir <private-backups> --actor <actor> --output <result>`.
The utility archives the entire failed generation, marks the old permission
`superseded_unentered`, adds one permission for the following generation, and
returns the original job to ready without changing its generation or attempts.
Normal claim admission increments generation; the first actual upload consumes
permission and increments attempts. It does not apply to arbitrary unknowns.

The two-database journal uses staged/armed/committed states. Repeating the same
plan resumes a crash or is a no-op once committed. A changed execution fact
fails closed. Backups preserve evidence only; restoring them over live facts is
not a supported rollback. Current native fences still reject old generations.

Validation: `python3 -m unittest scripts.test_x_unentered_recovery scripts.test_x_media_interrupted_recovery`.
Also rehearse the exact private plan on database copies, including interrupted
staged/armed states and repeated apply; verify old claims, operations, receipts,
queue/log identity and attempt counts are unchanged.

Separate reservation means a worker may own a central claim even though native
admission failed. Its failure receipt must not be discarded. For the exact
reviewed unentered plan, use `stage-receipt-ack --plan <plan> --backup-dir <backup>
--actor <actor> --output <result>` before deploying the Tag bridge. This archives
explicit source/generation/claim-hash permissions in the native ledger. The
bridge may then acknowledge only those exact false-unknown failure receipts
while an unchanged original recovery permission remains armed and no native
claim/operation/checkpoint exists in that generation. It performs no native
failure or platform write. Normal coordinator delivery acknowledgement remains
responsible for durable ACK and dedupe. Rebuild the continuation plan after all
current-generation failure deliveries have been accepted; `apply` requires one
such acknowledged delivery for every job before arming another generation.
