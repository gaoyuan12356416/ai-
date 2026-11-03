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
