# Tag recovery stopped before native admission

The Tag bridge now checks the immutable Auto task before reservation. Production
runs the publisher as `x-post-automation`, while its Auto database directory is
owned by `x-post-daily` with mode 0700. Root-only diagnostics concealed this:
`hit_timing.assert_due` could not open the read-only origin under the actual
publisher UID. It returned the generic `x_tag_dispatch_failed` before native
claim creation. Existing manual tasks bypass this Auto-origin read.

Deploy `scripts/repair_x_tag_origin_access.py apply --directory <private-record>`
from an exact pushed GitHub checkout. It backs up ACLs, grants only directory
traversal and database read access to the publisher, then tests SQLite `mode=ro`
as that user. No restart or scheduler interruption is required. Roll back with
`python3 <same-checkout>/scripts/repair_x_tag_origin_access.py rollback --directory <private-record>`.
Never restore an old business database to undo this configuration fix.

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
