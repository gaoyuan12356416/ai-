# Paused backlog operator

`scripts/pipeline_backlog_dispose.py` consumes the privately frozen inventory
of paused sources. It is not a general retry interface. Keep the inventory,
plan, database backups and execution reports outside Git. Files created by the
tool use exclusive creation and mode 0600; the backup directory must be below
the coordinator database directory on the data disk.

1. Run `plan --config CONFIG --snapshot SNAPSHOT --snapshot-sha256 SHA --output PLAN`.
   This is read-only. Review the exact source identities, original publication
   dates, requested actions, per-source proof hashes and blocked sources.
2. Before applying or recovering any pending X auto source, stop only the
   independent X auto metadata planner service and its runner/scheduler
   triggers. Let accepted metadata requests finish first. The operator checks
   that all named units are loaded and inactive, with zero service MainPID.
   It does not stop services itself. Keep the X publishing sidecar and rendering
   nodes running. Retain the original unit states for restoration afterward.
3. Run `apply --config CONFIG --plan PLAN --plan-sha256 SHA --backup-dir DIR
   --actor OPERATOR --output REPORT`. Use a new report path each time. SQLite
   backup, integrity checking and SHA recording precede the first item. Each
   item is independently revalidated and transacted; a blocked item does not
   reset other work. An exit code of 2 means the durable report has blocked
   sources that need review.
4. Read back the report and native/central audit rows before restoring the
   metadata planner. Restore its recorded prior states only after all intended
   native-first commits have either completed centrally or been investigated.

Resume records native authorization and removes only the exact source hold.
Central admission follows only after native readback and matching ownership,
material, schedule and stage proofs. Planner-only parents stay unavailable to
nodes. Their exact children need matching native and central authorization.
Archive uses each channel's cancellation state, retains its hold, artifacts,
material reservations and receipts, and archives the untouched central stage.
The separately proven stale preparation of an already published source changes
only the central preparation observation.

If a process stops between native and central commits, reuse the **same**
approved plan with a new report path and keep the X auto planner quiescent.
The native journal prevents a second native action; the original whole-row
postcommit proof must still match before central admission. Do not regenerate
a plan to hide a drift, clear a lock or delete a journal. Completed journal
replay does not require stopping the planner and never resets later work.
Only central `updated_at` and `source_status` collector observations may drift;
all native source/evidence and other central fields remain strictly checked.

The tool never calls rendering, upload or platform APIs, changes original
publication schedules, or retries failed/unknown work. Schema differences in
a related evidence table block that source explicitly instead of omitting its
evidence. No missing table is created except the additive disposition/journal
tables during an authorized apply.
