# Meta Page recovery, 2026-11-17

The hourly scanner owns Page pools 24 (Dramawave), 25 (FreeReels) and 57
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
the next hourly scan continues. Receipted creative work retains its credential
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

Each completed scan/execution batch freezes one summary, including empty and
failed scans, for group `oc_b633b6fba2c78f43ef1ebacd314d518c`. A batch does not wait
for Meta review. Changed, reviewing, deliverable, failed, unknown, waiting and
skipped counts remain separate. Frozen UUID + outbox receipts prevent duplicate
known sends; an uncertain send is retained for readback. Notification failure
does not block recovery. No socket-bot configuration is changed.

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
Completed historical SELECT partitions are cached with the exact scope, UTC
window, SQL and collection start time so observed transport failures can resume.
Failed reads are never cached. A fully published collection starts fresh on the
next refresh; only the last two completed disposable caches are retained.
The original 1 GiB / 30-minute portrait unit budget predates the expanded scope.
`ops/meta-page-recovery/portrait_capacity.py` backs up and sets only that unit to
3 GiB / 75 minutes, without restarting the active collector. Its temporary cgroup
overlay is restored after the current collection. Capacity rollback drains only
this affected oneshot and never kills work or restores old business snapshots.

## GitHub-first deployment

Run focused tests and compilation, push `codex/meta-page-recovery-20261117`, then
fetch and checkout that exact commit on CPU. Install without activating:

```
python3 CHECKOUT/ops/meta-page-recovery/deploy.py install --source CHECKOUT --commit SHA --deployment DEPLOYMENT
```

Complete read-only preview and marked Feishu acceptance before activation.
Profile refresh is independent: while it runs, use the last verified snapshot
and the agreed unprofiled fallback; do not block unrelated accounts globally.
`deploy.py activate --deployment DEPLOYMENT` establishes the
baseline, then enables `meta-page-recovery-worker.service` and
`meta-page-recovery-scan.timer` (hourly Beijing :05). Do not exercise actual swaps
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
