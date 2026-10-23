# Facebook Page material pool blacklist waiver

The operator requested removal of the blacklist restriction in the Page material
pool. Pool ID resolution, preview, admission, scheduled selection and execution
now waive both content_id and series_code blacklist checks. Pool release time,
language, metadata, media, Page authorization, capacity, repeat confirmation and
unknown-outcome protections retain their existing policies. Manual/Excel policy
and old automatic publishing are unchanged. SourceRepository remains strict by
default, and unknown worker task kinds do not inherit the waiver.

Only pool.py, pool_scheduler.py and worker.py are deployed to the FB and main
runtime copies. No schema changes, edits to blacklist data, historical retries,
test Posts or automatic insertion of the screenshot IDs are part of this change.

Deploy scripts/deploy_fb_pool_blacklist.py from the exact pushed GitHub checkout,
first dry, then --apply. It verifies live baseline hashes, avoids enabled pool
time points, drains publishers, backs up code and SQLite, and restores saved
service/timer states. Only the FB sidecar restarts; the main API stays running.
If services fail after new code may have accepted items, retain new code and
reconcile service state before further action.

Rollback: run the same script with --rollback <manifest.json>. It refuses while
pending/processing pool items or active/unknown pool tasks remain, both before
and after draining. Preserve current SQLite, task snapshots and all short links.
Never restore the database backup over accepted publication facts.

Validation uses fake Graph and temporary SQLite for both blacklist types across
the complete pool lifecycle, plus current regression suites. Production checks
use the running pool resolve/preview endpoints only and verify the screenshot
IDs without adding them or publishing.
