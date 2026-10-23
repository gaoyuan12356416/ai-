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

The unchanged effects collector runs independently of this sidecar and writes
only old effect tables. Its four entry/dependency files must match reviewed
release hashes; then its active process is excluded from the drain check. All
actual publisher/preparer units and task leases still drain. The first attempt
under the generic drain safely restored states without changing runtime code.

Rollback: run the same script with --rollback <manifest.json>. It refuses while
pending/processing pool items or active/unknown pool tasks remain, both before
and after draining. Preserve current SQLite, task snapshots and all short links.
Never restore the database backup over accepted publication facts.

Validation uses fake Graph and temporary SQLite for both blacklist types across
the complete pool lifecycle, plus current regression suites. Production checks
use the running pool resolve/preview endpoints only and verify the screenshot
IDs without adding them or publishing.

## Verified release

- Runtime GitHub commit: `101fac11ad6f15ef1cfad450be5a07e86aa46159` on
  `codex/fb-pool-blacklist-20261023`; business-code commit `b2ce577`.
- Host: `43.166.187.96`; release checkout
  `/mnt/data-disk/fb-video-publish/releases/101fac1`; runtime copies
  `/opt/fb-auto-post/current` and `/root/drama_material_service`.
- Backup manifest:
  `/mnt/data-disk/fb-video-publish/backups/pool-blacklist-20261023T084932Z-101fac11/manifest.json`.
  Its online `operational.sqlite3` backup is evidence, never a database restore target.
- Six installed file hashes matched; no schema migration or main API restart by
  this installer. FB sidecar, worker and pool scheduler are active; saved unit
  active/enabled states were restored. Independent effects collection completed
  normally (exit status 0). Other operators were active during the work: the
  number of enabled pools changed from one to three before apply, so the original
  screenshot's paused state is not claimed as a post-deployment invariant.
- Real PurpleDrama pool resolve before: 3 total, 0 valid, 3 material_blacklisted.
  After: 3 total, 3 valid, 0 skipped; separate pool preview returned all 3.
  Material IDs: 7781057, 7778595, 7725890. Checked at 2026-10-23 08:50:43 UTC.
- Verification preserved the Page's config and pool tables byte-for-byte across
  the read-only requests. All 114 pre-existing task snapshots/frozen fields and
  114 link records matched the online backup; SQLite quick_check=ok.
- Public Page UI 200, unauthenticated pool API 401, private broker 404.
- Regression runs passed: pool blacklist lifecycle 35, deployment/rollback 4,
  core 49, manual policy 49, Page pool 44, manual blacklist 45, pool IDs 12,
  pool identity 19 (one pre-existing skipped test). Counts include inherited
  fixtures and are not unique cases. Python compilation and diff checks passed.
- Durable evidence:
  `/mnt/data-disk/fb-video-publish/evidence/pool-blacklist-before-20261023.json`
  and `pool-blacklist-after-20261023.json` in the same directory.
- First attempt manifest ending `20261023T084523Z-b2ce5779` is `failed_restored`
  with no runtime edits. Use the successful manifest above for rollback.
- Maintenance skill reference updated with this pool-only policy change.

Exact rollback (only after pending/processing/unknown pool work is reconciled):

```bash
python3 /mnt/data-disk/fb-video-publish/releases/101fac1/scripts/deploy_fb_pool_blacklist.py --rollback /mnt/data-disk/fb-video-publish/backups/pool-blacklist-20261023T084932Z-101fac11/manifest.json
```

Keep current SQLite and links. The command checks pool work both before and after
stopping producers; never bypass its refusal by deleting ledger or queue records.
