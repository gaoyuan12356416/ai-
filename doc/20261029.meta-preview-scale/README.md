# Large Meta asset previews

The previous whole-job 10,000-Ad cap discarded complete scans. Each selected
product now retains the existing 5,000-row keyset pages and complete watermark,
count, lineage and ownership checks, without a cumulative 10,000-row rejection.
Up to four product reads share the source's existing FIFO/SQL capacity.

Preview Meta reads use independent account workers under the eight-operation
budget, with at most 50 exact IDs per GET. Read data is consumed once, and an
unsupported or incomplete batch falls back to the ordinary node read. Execution
uses fresh clients and the original per-object checks, phase barriers, durable
claims, credentials, reference checks and uncertain-outcome fencing.

Task detail reads decode only the requested page and its Video account results,
with summary counts and page rows from one SQLite snapshot. Historical blocked
Videos still appear in the pending filter. Polling no longer locks page controls;
switching requests aborts the old read and ignores stale responses. Editing the
scope permits a new preview while the old task remains in history. Old cap errors
can be explicitly re-previewed with their frozen scope.

`scripts/run_meta_asset_scope_once.py` is an operator tool, not a timer. It binds
only the asset package to the verified release, uses the original owner's live
Cookie/module/product permissions, previews an explicit subset of the source
task's products, and submits an explicitly authorized execution through the main
API so all DELETEs share its existing budget. It never repeats an unknown write
or changes the operation ID. Results and SQLite evidence stay on the data disk.

Validation: 368 Python module tests, two operator-continuation tests, 19 actual-source DOM tests, syntax checks,
and a real-browser local 15,000-object fixture. No fixture test sends a deletion.
Live operator execution and final receipts are separate acceptance evidence.

Deploy via `scripts/deploy_meta_preview_scale.py prepare RELEASE`. Preserve its
printed backup, confirm no active deletion/recheck, stop only the main API,
apply the plan, then use `scripts/safe_restart_drama_api.sh`. Independent read-only
previews survive; main-process previews recover as interrupted, without writes.
Both HTML/JS copies change; shared navigation, other services and timers do not.

Rollback: with execution idle, stop `drama-material-api.service`, run
`python3 RELEASE/scripts/deploy_meta_preview_scale.py rollback BACKUP`, then
`bash RELEASE/scripts/safe_restart_drama_api.sh`. Keep the live SQLite ledger.
Restoring code never restores old task states or repeats DELETEs.

## Production verification

Deployed `16eb6d9e8277983264da8cac1e87b5ff3d1b990e` to
`43.166.187.96:/root/drama_material_service` at 2026-10-29 15:40 CST.
Only the main API restarted (PID 1339234 → 1594216); the independent
read-only recovery worker remained alive. All eight target hashes, including
the two public static copies, match the verified release. Public HTML and JS
return 200 with the new cache version. Owner-authenticated detail reads return
200, and the old main-process preview correctly recovers as interrupted.

Backup: `/mnt/data-disk/meta-ad-asset-delete/recovery-backup-20261029T073951Z-16eb6d9e8277`.
API/public acceptance JSON is stored there. Deployment preserved the existing
360,020 object attempts, 332,381 receipts and 1,080 video-account attempts.
These are deployment checks, separate from subsequent operator-authorized
cleanup receipts.

Exact rollback, only after deletion/recheck workers are idle:

```bash
systemctl stop drama-material-api.service
python3 /mnt/data-disk/meta-ad-asset-delete/release-16eb6d9e8277/scripts/deploy_meta_preview_scale.py rollback /mnt/data-disk/meta-ad-asset-delete/recovery-backup-20261029T073951Z-16eb6d9e8277
bash /mnt/data-disk/meta-ad-asset-delete/release-16eb6d9e8277/scripts/safe_restart_drama_api.sh
```

Do not restore the evidence SQLite copy.

## Creative references and durable continuation

A large verified Creative set also exposed a 60-second source-reference timeout.
Verified references now split by exact account and at most 500 Creative IDs,
using at most four source readers. Cross-product references remain in the query;
unverified identities retain the original fallback. A live read-only probe
checked 8,377 Creatives across 127 accounts in 60.051 seconds, with 2,563 source
references and no out-of-scope Ad references. Meta verification is still required.
Blocked-object rechecks now use the existing bounded account GET workers, with
fresh authorization for each object and unchanged frozen IDs and write fences.

The follow-up release is `56b14d17c3dc42def600b2b22f8b763b3c83976c`.
As of 2026-10-29 16:47 CST it is staged, not yet loaded by the main API: the
operator-authorized Video phase is still running. The one-time
`meta-scope-continuation-20261029.service` waits for that run and every other
asset deletion/recheck to settle, backs up and replaces only source/service,
restarts only the main API, verifies hashes/health, then submits the frozen
recheck and Creative-only continuation through the normal API with stable IDs.
It has `Restart=no`; failures require checking the ledger before resuming.
The controller JSON records the actual commit, backup, PID and deployment stage.

The one-time `meta-scope-video-audit-20261029.service` waits for this continuation,
then uses GET-only complete account-video inventories to distinguish absent,
still-present and unverified associations after failed DELETEs. It preserves
the original DELETE ledger and writes a separate evidence file; an error alone
never becomes success. Script release: `825002529fc7bbd0552db4bd341b91c0c27b11c3`.
Neither service installs a timer or a recurring deletion job.

For this reference layer, use `scripts/deploy_meta_reference_scale.py prepare`
and the controller's recorded backup. To roll back after execution is idle,
stop the main API, run the same release's
`scripts/deploy_meta_reference_scale.py rollback BACKUP`, then restart with
`scripts/safe_restart_drama_api.sh`. Roll back this layer before attempting the
earlier full preview rollback. Retain the current SQLite database throughout.

## Explicit approval of original Creatives after parent Ad replacement

On October 29 the user additionally authorized the 1,278 Creatives marked
`ad_ownership_unverified`. Their own accounts and video relationships were
verified; each related blocked Ad has the specific `creative_changed` reason.
The protected operational manifest freezes these original Creative keys, owner,
preview and a SHA-256 digest. It does not include replacement Creatives or approve
deleting the changed Ads.

The normal recheck API accepts an optional exact
`approved_changed_parent_creative_keys` list. Within the recheck claim transaction,
the store checks frozen membership, Creative proof and the exact parent reason,
then records the actor, request ID and allowed parent references per object.
Idempotent requests must retain the same key fingerprint. These approvals only
waive references from those original changed parents for those specific old
Creatives. Fresh Creative ownership/video checks, other source/Meta references,
permissions, global dedupe and uncertain-result fencing remain in force.
Unknown or successful objects cannot be force-released by approval.

The controller takes `--creative-approval` and uses the stable v2 continuation
request prefix; only the Creative phase is executed. This release adds `store.py`
to the scoped source/service rollout, with the same 16eb baseline and no schema
change. Tests cover exact-ID deletion, unchanged Ad/replacement scope, idempotency,
rejected approval scopes, fresh Meta failures and newly appearing outside
references before execution.

The waiting controller was replaced at 2026-10-29 17:16 CST with verified release
`a7176fbebe308138b66213b7356ef99bde9522ab` and the protected approval manifest.
It records 1,278 approved keys and digest
`6ee8c8b97b60b8801ee55e61129d72caa7ee26a1b344e353ac9ec99f574c738e`.
All 365 Python tests passed locally and on the CPU server. An isolated SQLite
rehearsal using the real 1,278 Creatives and their 1,278 parent Ads verified the
approval without changing live rows or making any Meta call. Both waiting
services were replaced; the active Video run and main API PID 1594216 continued.
The main API still uses 16eb until this staged controller reaches its idle rollout.

## Pending-only recovery after the API interruption

A systemd stop/start at 17:18:07 interrupted the original Video run before the
controller's own idle restart at 17:18:35. The initiating actor is unconfirmed;
the journal shows an orderly restart rather than an OOM. At 17:24, 6,722 Videos
remained pending and four had unknown outcomes. The controller deployed a717
successfully and started the authorized Creative recheck. Its backup is
`/mnt/data-disk/meta-ad-asset-delete/recovery-backup-20261029T091821Z-a7176fbebe30`.

The execution API now accepts an explicit boolean `only_pending` for recovery.
It retains the complete frozen scope while executing only pending objects and
pending Video-account pairs. Successful, blocked, failed and unknown outcomes
remain untouched. The filter is recorded durably in the run, preserved when it
finishes, and included in request-id conflict checks. The default execution
behavior is unchanged. All 367 Python tests pass, including interrupted-run
recovery that preserves failed records and unknown fences.

`scripts/resume_meta_pending_video_once.py` waits for the current Creative
continuation and first complete Video audit, then waits for all asset writes to
be idle. Its selected service/store rollout uses the a717 baseline and a fresh
backup. The script reconciles unknowns with GET-only calls, submits one stable
pending-only Video run through the main API, and performs a new final read-only
account audit. It never automatically retries another interruption.

The pending-recovery layer rolls back with
`python3 RELEASE/scripts/deploy_meta_pending_resume.py rollback BACKUP` while
the main API is stopped and all writes are idle, followed by
`bash RELEASE/scripts/safe_restart_drama_api.sh`. Roll back this layer before
the a717 reference/approval layer, and retain the live ledger throughout.

## Deployed pending recovery and interrupted monitor handling (18:10 CST)

Runtime `2f202a00434cdac948def67dcdab26fa61487591` was deployed at 17:55:46
after all asset writes and rechecks were idle. Only source/service/store changed;
the main API restarted from PID 1866164 to 1942336. Health and real-owner detail
returned 200, all three hashes matched, and object states, attempts, receipts and
Video account attempts were unchanged. Backup:
`/mnt/data-disk/meta-ad-asset-delete/recovery-backup-20261029T095244Z-2f202a00434c`.
Evidence is `pending-api-deployment.json` in the operational audit directory.

The earlier recheck stopped at checked=2,432/released=2,343 after a MySQL 2013
connection loss in the exact four-product ACL query. The source now applies its
existing bounded transport-error retries to each fresh ACL read. It never caches
permissions or retries an authorization denial. All 368 module tests passed
locally and on the server.

A separate API restart at 17:58:50 interrupted the v3 read-only recheck after
97 checks and eight further releases. The initiator remains unconfirmed. The
three scoped runtime hashes still match 2f; new API PID is 1946048. A real-owner
GET recovered the dead recheck owner as interrupted, preserving all progress.
The one-time monitor now polls this bounded GET as well as SQLite, so a restart
cannot leave it waiting forever on a stale running row. It allows at most three
read-only passes for known process/transport interruptions. Authorization errors
and uncertain DELETEs are not retried. Creative execution is pending-only and a
second write interruption cannot be reported as finished. Two focused tests
verify GET-only bounded retries and rejection of unknown or authorization errors.

The resumed operation prefix is `scope-recovery-20261029-reference-v4`.
All 1,278 exact approvals are already persisted: do not resubmit the manifest
when restoring a recheck. The Video recovery prefix remains
`scope-recovery-20261029-video-pending-v1`; no run has yet been submitted for it.
Its `--deployed-backup` option verifies both the current runtime and the selected
release against the deployment hashes, avoiding another API restart.

The ordered pipeline is Creative continuation, first GET-only full account
inventory, pending-only Video execution, then a new final inventory. Completion
requires `pending-video-recovery.json` and `video-absence-audit-final.json` both
finished and no pending/in-progress objects. Failed DELETEs, separate absence
evidence and unknown outcomes retain their distinct meanings.

Latest-layer rollback, only with all writes/rechecks idle:

```bash
systemctl stop drama-material-api.service
python3 /mnt/data-disk/meta-ad-asset-delete/release-2f202a00434c/scripts/deploy_meta_pending_resume.py rollback /mnt/data-disk/meta-ad-asset-delete/recovery-backup-20261029T095244Z-2f202a00434c
bash /mnt/data-disk/meta-ad-asset-delete/release-2f202a00434c/scripts/safe_restart_drama_api.sh
```

For a full layered rollback, apply this before the a717 reference backup
(`recovery-backup-20261029T091821Z-a7176fbebe30`) and the original 16eb backup.
Never restore an old ledger over the current deletion evidence.

## Receipt-backed Creative recovery

The operator approved completing the current recheck, repairing both remaining
recovery cases, deleting eligible original objects and auditing/recovering Video
failures. The waiting controllers were stopped while the main API's v4 recheck
continued unchanged; no active DELETE run was interrupted.

Sixty-five Creatives inherited `execution_stopped` during the previous SQL
interruption, with zero deletion attempts. Explicit rechecks now reconsider this
temporary reason through the normal fresh ACL/reference/Meta checks. A cancelled
read no longer overwrites an unexamined object's original blocker.

Some original Creatives lost a video reference after this cleanup had successfully
removed that video from the same account. Requiring exact equality with the old
video list then blocked cleanup of the original Creative itself. Both recheck and
execution now accept a strict reduction only when every missing video has a
confirmed account-specific outcome in this frozen job's ledger. New video IDs,
missing evidence, failed/unknown outcomes, wrong accounts and outside references
remain blocked. Frozen IDs and verified video lists are never rewritten. The
successful read proof lists the missing IDs and the account supplying evidence.

Validation: 375 module tests and two monitor tests. Added cases cover a real
temporary SQLite account receipt through Video deletion, Creative recheck and
fresh execution; stopped reads; wrong/unknown/global receipts; new video IDs;
and live ownership/reference conflicts. No test sends Meta requests.

Deployment uses `scripts/deploy_meta_creative_recovery.py` with baseline
`2f202a00434cdac948def67dcdab26fa61487591` and only graph/service targets. Wait for
all asset writes and rechecks to finish, prepare a fresh backup, then apply with
the main API stopped and use `safe_restart_drama_api.sh`. Rollback uses this
wrapper's `rollback BACKUP` before the earlier 2f/a717/16eb layers; keep SQLite.
The pending Video controller accepts this verified two-file backup and compares
all four relevant runtime modules to its release, avoiding another API restart.
