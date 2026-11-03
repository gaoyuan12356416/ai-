# 2026-11-03: saved capacity, failures and dated backlog

## Requested behavior and scope

The operator requested confirmation of saved capacity, diagnosis of 87 failed
stage records, resumption of the backlog originally scheduled on November 2 in
Asia/Shanghai, and disposal of older paused work. Disposal means cancellation
and archival, preserving publication receipts, material reservations and dedup
history. It never means database deletion or resetting uncertain publication.

The 03:40:27Z read-only snapshot contains 1,456 paused stage rows / sources:

| Action | Sources |
| --- | ---: |
| Resume November 2 X auto | 248 |
| Resume November 2 X queue | 15 |
| Resume November 2 TikTok auto | 18 |
| Cancel/archive older untouched sources | 1,173 |
| Archive obsolete preparation observations of already published X sources | 2 |

Scope uses original publication time, UTC `[2026-11-01T16:00Z,
2026-11-02T16:00Z)`, not the preparation eligibility or creation date. The
scope and each database row are frozen and hashed before any modification.
November 3 YouTube cover work is separately coordinated and excluded.

## Design and review boundaries

Native and central ledgers retain their existing roles. An additive
`pipeline_backlog_disposition` records the exact source identity, immutable
original account/schedule/template/material fields, action and approved plan
hash. A source-level journal records native and central commits independently.
The native transaction retains artifacts and material ledger records. Resume
removes exactly one canonical hold; archive cancels the native untouched row
while retaining its hold. Central commit admits only the projected correct
phase, with generation zero and no claim, platform intent, receipt or lock.
An archive is an explicit terminal observation, never a fabricated success.

The X auto parent remains planner-only until its canonical queue exists.
Only an exact child linked by native `execution_queue_id`, matching account
and material, inherits a matching committed native AND central authorization.
This also applies to prepare-to-publish transitions. Native-first crashes do
not open central admission; idempotent journal replay finishes the same source.
Neither a batch, template nor account receives a general cutoff exemption.

Existing approvals, template validation, episode ordering, scheduling,
unknown-result account fences and native dedup checks remain effective. A
drifted or executing item is blocked individually instead of being reset.

## Current capacity and GPU diagnosis

At 03:55Z, saved configuration version55 retained CPU ceilings4/4, GPU2/4,
global publication8, and channel X8/FB8/TT4/YouTube2. GPU effective targets
were1/1 after adaptive reduction; saving a ceiling does not force a target.
Its six-hour successful samples were preparation2/publication1, below the
three required for expansion. CPU targets were4/4.

The read-only selection reconstruction found25 GPU-compatible preparation
jobs (TT22/FB3) blocked by historical uncertain publication account/Page
fences, one similarly blocked publication, seven disabled YouTube cover
tasks, and two TT jobs with frozen CPU-only profiles. GPU CPU/GPU/encoder
usage was0.5%/0%/0%, with95% available host memory. This is admission and
account availability, not exhausted GPU capacity. No lock was cleared to
increase utilization.

## Failure accounting

The11:40 Beijing snapshot contains87 failed stage records across85 sources:
X bridge failures70 (prepare66/publish4), X dispatch failures2, X account
failures3, TikTok not-claimable8, Facebook not-claimable3, and YouTube cover
failure1. Of these86 preceded the latest restoration and one X publication
admission failure was new. No central platform-operation intent was present
in these87 rows. This does not imply every underlying account is safe: the
three Facebook Pages have earlier uncertain publications, and the YouTube
task has a later successful approved revision that must not be repeated.

Separate diagnosis of new X admission failure found repeated XPostStore
initialization holding SQLite write locks during status queries. A live
nonblocking stack showed one historical migration loop and three waiters in
BEGIN IMMEDIATE. The exact original exception stack for queue5458 was not
retained, so the adjacent lock exception must not be represented as its
direct stack. Its frozen render contract passed isolated offline validation.

## Verification and deployment

Real SQLite tests cover native exact-identity admission, cross-node exclusive
claim, phased/child admission, archive suppression, wrong authorization,
row drift, platform evidence, planner-only parents, and interruption replay.
Operator tests additionally verify backups and preservation of unrelated
running work. Only normal authorized work supplies production acceptance;
no test posts are created.

Deploy exact GitHub versions, back up databases to the CPU data disk with
SQLite backup/quick_check and hashes, and use a narrow protected control
service restart. Rendering nodes and the main API are not restarted.
Apply the hash-frozen plan item by item. Read back native/central disposition,
unmodified active leases and publication facts before reporting completion.

Rollback first stops new admission and allows current work to finish. Restore
compatible code/configuration only; never restore an old database over new
receipts or automatically recreate cancelled work. Retain the additive
disposition, journal, holds, frozen artifacts and all publication history.

Deployment and application are pending at the time of this design record;
append actual SHAs, plan hashes, counts and business readback after execution.

## Production execution and independent readback

At12:11 Beijing, coordinator `b6e205a879ac14da4f2e4b61a950781e2d22f30d`
passed61 targeted tests on production Linux/Python3.9 and replaced the previous
version after HTTP handlers drained. The connection gate lasted1.009 seconds,
was removed, and health returned200. Configuration was identical before/after.

Native X/Xauto and TTauto received only the reviewed patches from `d69a369`,
preserving all other production files, particularly TT publisher SHA
`da713109c6630fea1df7bba314053ee9f1ad9b837207a93ebf9a8785719a6afb`.
Operational helper `80152c7` handles an already-failed metadata oneshot and
the service-listener startup race without repeating a successful restart.
Both rendering node supervisor PIDs stayed unchanged. Accepted HTTP requests
and platform uploads were not terminated. Temporary firewall rules are absent.

Approved plan SHA:
`fffafb8fb32f7449c7bb3980d6362627d6c389962c0ab63e2521d55b24bcc9f3`.
All1,456 source actions committed with zero blocked items. At12:20:37,
X/TT admission and the X metadata planner were restored. Independent readback
confirmed281 resumes,1,175 archives, zero central paused backlog and1,173
retained native holds. Both already-published ghosts retained exactly the
original published rows and publication facts. Archived material, artifacts
and associated ledgers match the backups.

The18 TT index times became their approved preparation eligibility times;
their native original publication appointments and frozen identities did not
change. Four resumed X queues5348–5351 made their first generation-one claim
and actually rendered on CPU(three) and GPU(one). The other GPU slot rendered
TT simultaneously. This is resumed processing, not a claim that281 posts
have already been published. One X account requires review and eight initial
X selections had no eligible material; their normal business gates remain.

Monitoring summary/workers/tasks returned200 in170/10/28ms. All displayed
counters matched the ledger, unique success counting matched publication
facts, and both tested ordinary users had zero out-of-scope rows.

Backup/evidence locations on CPU43.166.187.96:

- `/mnt/data-disk/pipeline-dispatch/backup-coordinator-20261103T041127Z`
- `/mnt/data-disk/pipeline-dispatch/backlog-20261103/` (private plan, apply report,
  per-database backup manifest, maintenance journal)
- `/mnt/data-disk/pipeline-dispatch-runtime/backlog-native-d69a369660/`
  (exact patch trees, original source/ledger backups, separate deployment attempts)

## Explicit manual GPU capacity and duplicate failure acknowledgements

The later user instruction explicitly requests GPU concurrency to follow the
saved values. A one-time target raise was insufficient: automatic CPU-pressure
control reduced it again. `59aa673` therefore adds explicit per-pool manual
mode. GPU preparation2/publication4 will be configured manually; CPU retains
automatic control. In manual mode, the existing numeric save changes the
target to the saved ceiling; lower targets still allow in-flight work to finish.
The page displays manual/automatic mode and uses its existing authenticated
save payload, so the main API needs no restart. Explicit mode switching uses
the protected coordinator administrator endpoint; no nonfunctional UI control
or gateway bypass is introduced.

`858d431` fixes the old X5458 failure receipt acknowledgement. The same claim,
generation and error must already be failed with no external intent, operation,
checkpoint, receipt, publication, canonical result or native-attempt evidence.
Only an idempotent delivery acknowledgement is added; no task, resource fence,
timestamp or performance sample is rewritten and no platform retry occurs.
Eight real HTTP/SQLite cases and39 related tests passed. Manual capacity and
existing adaptive behavior passed48 related tests plus local desktop/mobile
browser checks. The combined core/capacity/coordinator follow-up passed58 tests.

At12:32:20 Beijing, exact coordinator version
`d3920082c0e0084f1c40632b0409b6bba6ecfafb` passed76 targeted tests on Linux in
14.345 seconds and was deployed with a1.482-second guarded HTTP drain/switch.
Health200, no remaining gate, main API and both node supervisors unchanged.
Backup: `/mnt/data-disk/pipeline-dispatch/backup-coordinator-20261103T043202Z`.

At12:32:44, two authenticated administrator capacity requests returned200 and
configuration versions65/66. Only HK GPU modes/targets changed: preparation
manual2/2, publication manual4/4. CPU remained automatic4/4, all channel/global
limits, cutovers and enabled-channel flags remained unchanged. Subsequent
readback after adaptive evaluations retained the exact manual values.

At12:34:36, GPU had two real busy preparation processes and four live idle
publication processes awaiting ready eligible work; CPU had four actual
preparations. CPU outbox137/137 acknowledged, zero pending. Old job31715
retained its original failed status, generation1 and exact03:35:40.680Z
timestamps, with no operation and exactly one new delivery acknowledgement.

Both main-API static copies and the separate Nginx public copies of monitor
HTML/JS now match the exact GitHub bytes, with public HTTP200/hash verification.
No main-API or Nginx restart was needed. Backup:
`/mnt/data-disk/pipeline-dispatch/backlog-20261103/static-d3920082c0`.
Refreshing the page displays the manual/automatic label and versioned script.

The12:34 snapshot contains98 failed stages, up from the earlier87: all11 newly
observed failures are X native account gates (eight require account review,
three account-not-publishable), with zero publishing attempts/unknown results
and no execution queue created. These are not new write-lock failures and were
not retried or bypassed. Their accounts are19/23/46/102(review) and123(unavailable).
Some restored selections also have no eligible material. All1,456 dispositions
remain committed, paused backlog remains zero, and normal authorized work
continues in the existing workers; the backlog is not represented as fully
published merely because it was released.

Rollback procedure: stop new claims for affected channels, let active work and
receipt delivery finish, then restore the prior code or native symlink from the
named deployment manifests. Never restore an old database over current work.
Keep dispositions, cancellation markers, receipts and material reservations.
An older coordinator does not implement manual capacity, so do not revert it
while assuming the manual-mode admission guarantee will remain active.
