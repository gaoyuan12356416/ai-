# Meta Page recovery, 2026-11-17

The four-hour scanner owns Page pools 24 (Dramawave), 25 (FreeReels) and 57
(HotDrama). The first successful scan creates a durable baseline and performs no
ad recovery. Later `status=1` / `forbidden_status=1` transitions create incidents;
a Page that recovered and becomes abnormal again has a new generation.

Only a configured ACTIVE Ad with ACTIVE parents and positive Meta `purchase`
actions over the account's today + yesterday can be changed. Report failures,
no data and zero purchases are distinct. Source SQL is FIFO gated, compressed,
read-only on 63350; all recovery state is local SQLite. Page mapping from SQL is
only discovery: Meta's current Ad and Creative are checked before every update.

An incident is pinned to its original pool. Same-language profile/unprofiled,
blank-language profile/unprofiled, then language/blank profile-relaxed stages
follow the existing selector order. Known direction conflicts are not described
as missing profiles. Unknown direction can match verified topic revenue shares.
Theme-positive top three candidates are ordered by active Ads plus current-run
allocations, then followers and numeric Page ID, ascending.

Original creative content, tracking, CTA, links and deep links are preserved.
An old Page's Instagram actor or post ID is never transplanted. Readable post
content is reconstructed; missing content fails only that Ad. Account-owned
media is reused; a known video reuse rejection allows one receipted copy of the
exact readable Meta source through resumable upload. Unknown writes are never
blindly repeated. Updating an Ad only sends its new `creative_id`, not status,
budget or parent changes. The same `ad_id` and reporting history remain.
Credential-scoped Page ADVERTISE/MANAGE tasks guide selection after account
access is checked. Missing/incomplete enumeration still permits actual Meta
validation. A rejected credential/Page pair does not exclude that Page from
other authorized credentials. Only three known-denied probes run per Ad attempt;
the next scheduled scan continues. Receipted creative work retains its credential
fingerprint (never the secret). Permissions are never bypassed or auto-granted.

## State and notifications

Data: `/mnt/data-disk/meta-page-recovery/state.sqlite3`, private media and
deployment records in the same data-disk root. SQLite WAL/FULL records write
intents and receipts separately. Crash recovery distinguishes an unconfirmed
POST from work that can safely retry. Accepted Creative receipts are reused.
Unknown Ad writes are reconciled by current Creative + Page + content; a lost
Creative receipt requires one exact operation name and payload match. A missing
read result is not evidence that a write failed. Graceful worker stop drains the
currently executing Ad in each account and leaves queued work intact.

Each completed batch freezes a local summary, including empty and failed scans.
Batch notifications are `recorded`, without sending them. Every Beijing day at
10:00, an independent timer freezes one report for the previous 10:00-to-10:00
interval, to group `oc_b633b6fba2c78f43ef1ebacd314d518c`. Recovery jobs retried in
several batches count once. Their outcome is the latest status at report build;
accepted Ad changes are separately counted by receipts within the interval.
In-flight batches, scan failures, no-data waits and true zero-PUR skips are visible
without waiting for all work or Meta review. Pool scope comes from the latest
observed scan. The date-based UUID and frozen outbox body prevent duplicate known
sends; uncertain sends remain protected. The independent report service delivers
at 10:00 even while recovery is busy; the worker can retry known rejections.
An atomic outbox claim prevents both owners sending the same card. Notification
failure does not block recovery.
Unsent legacy per-batch messages become `recorded` at worker startup, preserving
sent/unknown messages and all business receipts. No socket-bot configuration changes.

## Managed Page import and portraits

`python3 -m features.meta_page_recovery.import_pages --output RECEIPT` previews;
append `--apply` for the authorized one-time import. It uses the existing trusted
Gaoyuan identity and Page-management API, only adding normal, unconfigured Pages.
Existing configurations and manually deleted Pages are preserved. The online
SQLite backup and full before/after receipt remain on the data disk. Captions and
titles use the existing unique Gaoyuan template for each language, with English
fallback; an empty Page language stays empty. No publishing tasks are created.

The portrait collector covers managed Pages union current members of the three
pools and adds HotDrama's own revenue/catalog identity. The seven full UTC days,
D0 plus confirmed-social income, conservation and last-good-snapshot policy are
unchanged. Smaller 5k-source-row partitions, exact Page-set subdivision after
3024, and protocol compression repair the observed source timeout without
silently dropping data. Pool membership is frozen per snapshot; additions enter
the next refresh. Only the refresh unit's WorkingDirectory is overridden. The
portrait reader, main API and publishing workers are not restarted.
Completed paid-revenue and catalog SELECT partitions are cached with the exact
scope, UTC window, SQL and collection start time so observed failures can resume.
Failed reads are never cached. A fully published collection starts fresh on the
next refresh; only the last two completed disposable caches are retained.
Catalog batches use 25 content IDs and subdivide further on 3024/overflow; the
observed 100-ID resource read exceeded the source's existing 8-second limit.
Live Page state and recovery permissions are never served from this input cache.
The verified immutable social input and its original validation timestamp are
also frozen per collection. A continuation reuses the same input, with its hash,
coverage and original freshness proof; it does not relabel an old source as new.
Every new collection still enforces the original 3-hour social freshness rule.
Profile metadata exposes social validation/source times separately from build time.
Profile writes encode one Page at a time; only small payload hashes are retained
for the complete scope. PyMySQL consumes the record iterator in bounded SQL
batches inside one transaction. This repairs the observed 3-GiB cgroup OOM from
retaining every Page's JSON simultaneously. `refresh --resume-input VERSION.input.json`
reuses the original version key/input and reconciles existing rows by their exact
hashes. It refuses to replace an already published/superseding snapshot.
The original 1 GiB / 30-minute portrait unit budget predates the expanded scope.
`ops/meta-page-recovery/portrait_capacity.py` backs up and sets only that unit to
3 GiB / 2 hours, without restarting the active collector. Its temporary cgroup
overlay is restored after the current collection. Capacity rollback drains only
this affected oneshot and never kills work or restores old business snapshots.
The expanded cold collection and cross-region write were observed to exceed the
old 75-minute combined budget. Final hash verification reads 100 Page IDs at a
time and subdivides on 3024; every actual detail hash and global row count/cents
still must match. The database's existing per-query timeout is unchanged.

## GitHub-first deployment

Run focused tests and compilation, push `codex/meta-page-recovery-20261117`, then
fetch and checkout that exact commit on CPU. Install without activating:

```
python3 CHECKOUT/ops/meta-page-recovery/deploy.py install --source CHECKOUT --commit SHA --deployment DEPLOYMENT
```

Complete read-only preview and local daily-card verification before activation.
Profile refresh is independent: while it runs, use the last verified snapshot
and the agreed unprofiled fallback; do not block unrelated accounts globally.
`deploy.py activate --deployment DEPLOYMENT` establishes the
baseline, then enables `meta-page-recovery-worker.service` and
`meta-page-recovery-scan.timer` (Beijing 00/04/08/12/16/20:05), the worker and
`meta-page-recovery-report.timer` (daily Beijing 10:00). Do not exercise actual swaps
on historical banned Pages. `python3 -m features.meta_page_recovery status` gives
batch, job and notification state. There is no new public HTTP API.

```
python3 CHECKOUT/ops/meta-page-recovery/deploy.py rollback --deployment DEPLOYMENT
```

Rollback withdraws the new executor/timer and refresh override after graceful
drain, restores prior code/unit pointers, and keeps current configuration,
portraits, tokens, ledger, receipts and already confirmed Ad changes. Never
restore an old SQLite over newer business facts or automatically return Ads to a
blocked Page. Later release drift requires reconciliation before rollback.
Every installation freezes its own `DEPLOYMENT/deploy.py`. Use that copy for
rollback. For full feature withdrawal after several inactive pre-activation
installs, roll back deployment manifests newest to oldest until the first
manifest restores the original absent recovery units and portrait refresh
override. The scanner and worker are disabled; the live business databases and
already published portraits remain intact throughout.

For an existing live installation, use `deploy.py switch --deployment DEPLOYMENT`
after install. It retains the baseline, jobs and receipts, gracefully restarts only
the affected recovery worker and activates both timers without running an extra
scan. Its rollback restores the previous worker/timer activation states as well
as code/unit files. See [the schedule-change record](meta-page-recovery-schedule-20261117.md).
