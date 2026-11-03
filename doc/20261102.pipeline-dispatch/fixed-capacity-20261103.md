# 2026-11-03 explicit fixed capacity and idle queue diagnosis

The operator clarified the intended capacities at about 14:00 Beijing:

| Node | Preparation | Publication |
| --- | ---: | ---: |
| cpu-main | 4 | 6 |
| hk-gpu | 6 | 4 |

The inspected configuration was still version 66, with CPU automatic 4/4 and
GPU manual 2/4. At 14:07:18 the protected coordinator capacity API applied
versions 67 through 70. All four pools now use manual mode, with target equal
to ceiling. Global publication capacity changed from 8 to 10 to admit the
requested combined 6+4 publication processes. Channel limits, cutovers,
enabled channels and preparation-only channels were checked unchanged.

The operational before/after snapshots and individual request results are in
`/mnt/data-disk/pipeline-dispatch/backlog-20261103/capacity-20261103T060718Z`.
This was a runtime configuration change through the existing audited API; no
code deployment, node restart or ledger edit was needed to change capacity.

## Actual execution verification

At 14:09, both OS process trees and fresh authority heartbeats confirmed CPU
4 preparation + 6 publication processes, GPU 6 preparation + 4 publication
processes. CPU supervisor PID 810922 and GPU supervisor PID 53734 remained
unchanged, both with zero restarts. Both receipt outboxes had zero pending
receipts. The node version remained 95fdaa4c4db0ed19a465cb126d539274e95ea918,
and coordinator version remained d3920082c0e0084f1c40632b0409b6bba6ecfafb.

The original three active CPU preparations continued on their original workers
and generation 1. X queue 5484 completed preparation naturally and entered
publication as job 31832 at 14:07:34. No active work was killed or reassigned.
The momentary 2-preparation + 4-publication GPU observation, before 6 + 4
became visible, was propagation and process startup delay, not a local ceiling.

## Why idle capacity coexists with roughly 400 queued items

A complete read-only snapshot at 14:07:20 included **402 queued + 1 ready**.
Unlike the actual claim query, this inventory includes future reservations and
planner-only parent items. Its mutually exclusive first-blocker breakdown was:

| Reason | Items |
| --- | ---: |
| X Auto planner-only parent, not yet an executable child | 198 |
| Appointment or preparation eligibility time not reached | 111 |
| Account/Page held by an unresolved existing publication | 82 |
| TikTok native account conflict with an earlier prepared item | 5 |
| YouTube publication remains paused | 7 |
| Total queued plus ready | 403 |

The single ready item is included in the future-appointment category. At that
snapshot there were zero immediately claimable GPU preparations/publications.
There was no missing GPU capability, global/channel capacity bottleneck, or
claim scan-window truncation. GPU utilization was zero with ample free RAM and
VRAM; hardware saturation did not cause this idle snapshot.

TikTok items 4589/4591/4594/4598/4599 wait for their accounts' earlier prepared
items 4576/4578/4581/4585/4586, scheduled for 16:11 Beijing. The successors are
scheduled for 17:36 and their four-hour preparation windows are open, but the
native single-unfinished-item rule still applies. All five have zero publishing
attempts and no unknown outcome. Their 30-second admission deferral is distinct
from a failed publication or platform rate limit.

Unknown publication fences remain intact. Appointment times and account
serialization were not bypassed to occupy idle processes. YouTube configuration
and business tasks were outside this change. A live worker is a ready slot,
not proof that an eligible business task currently exists for it.

## Deeper X parent diagnosis and recovery scope

The first-blocker inventory above explains admission at that instant; it does
not prove the preceding tasks are healthy. Native-ledger checks at 14:13 found
197 of the 198 pending X parents blocked behind 65 earlier parents in
`retry_wait/reconcile`. Their canonical child queues were still `queued`, while
the central executor stages had failed. These parents continued observing the
nonterminal native queue every 30 seconds and could not unblock successors.

Of these sources, 61 had failed central preparation with
`canonical_bridge_unavailable`; publication failures are separate. Two of those
61 sources (queues 5392 and 5394) already have qualified prepared media and a
failed publication stage, so they are explicitly excluded from preparation
recovery. Only the remaining 59 untouched preparation candidates were reviewed
and restored against an exact private scope. No broad failed-task retry is allowed.

`03d8c90d0e41306e4d79d3d6179a57178bd28935` implements this exact preparation
recovery. It passed 25 real SQLite/native/concurrency/receipt tests locally and
the same 25 cases on Linux, with independent review. The 59 original tasks are
scheduled on **November 3 Beijing**, not the earlier November 2 paused-backlog
manifest. Restoring them also unblocks up to 180 later pending parent tasks.

The accepted scope hash is
`5b5bb2801dacf357e471e5a43f185f3def5b9c632d7dfe3cb65eb5b0d407aa3d`, and the
applied plan hash is
`93efecfdffe7d4201358be97507067e5121798130cf0abc54ea9873d6f0fac8c`.
Both nodes had no receipts, active claims or exact task scratch files for those
59 jobs immediately before application. Apply changed only their central
preparation admission, invalidated old claim tokens, retained original claim
generations/history and frozen recipe identities, and audited the old failure.
The next actual claim increments the generation normally. Native ledgers were
read-only and no service restarted.

Backup: `/mnt/data-disk/pipeline-dispatch/backlog-20261103/x-prepare-recovery-backups/x-prepare-93efecfdffe7d420-a8b676e1d7ea43b9bbd4900731bb82ef.sqlite3`.
At 14:24 both pools were actually processing CPU4 + GPU6 preparations. An
independent 14:26:26 snapshot confirmed all six GPU workers in `render`, CPU
three in artifact upload and one rendering. Of the 59 jobs, 44 were queued,
10 running and 5 had reached preparation-stage ready. The 15 newly claimed
jobs each gained exactly one claim at original generation +1. All original
identities, appointments, owners, failure events and claim history remained.
All 14 modern unknown-resource fences and 178 legacy unknown fences were
retained, and all six excluded publication failures remained unchanged.

The six existing-artifact sources were then handled separately using
`0c030a5c263e85a36e83f94ca93e4f3e2fada202`. The preparation/final-artifact suites
passed 16 cases locally, and all seven new artifact cases passed on Linux with
independent review. Twelve exact preparation/publication job identities had
no active execution or pending node receipts before the six-item application.
Native logs remained reserved with attempt zero, no media/Post IDs, no platform
operations and no unknown outcomes. The validated final render receipts,
source-file proof, recipe and frozen profile were bound to the recovery plan.

Plan: `cf9fd5f1c91458db684c577faca94b82ff2e9a958b92d7a9afd42f0dd4cefe6b`.
Backup: `/mnt/data-disk/pipeline-dispatch/backlog-20261103/x-first-publish-recovery-backups/x-prepared-publish-cf9fd5f1c91458db-a8ac4ad14d424e1e9228e24a70d23a3c.sqlite3`.
Only the six original publish jobs were readmitted with their existing final
artifacts. The two falsely failed preparation projections were confirmed from
the native ready receipts while preserving their original CPU producer IDs.
No native queue, recipe, material reservation, account fence or platform was
written by the recovery helper.

At 14:32:25 independent readback found each original publication at generation
2, exactly one media initialization/attempt, no unknown outcome, and uploading.
No preparation generation/checkpoint increased, no new queue appeared, and all
192 old unknown fences remained. Final artifact URL/SHA/size/MIME/duration/
profile/recipe matched exactly; two old GPU preparation records merely retained
additional native-result metadata. No final Post ID existed at this snapshot,
so these were not prematurely counted as successful publications.

The remaining orphan X planner 2383 had created the unique existing queue 5477
under run 1511, but had not confirmed its parent reservation. Both original
central records were generation-zero legacy observations with no node activity,
native preparation, platform attempt or receipt. Fresh no-handler evidence was
captured for both native HTTP services, together with exact two-node evidence.
`dfd19e42d4840ec856396ac7a1b755be968d2af5` expires only that orphan lease and
records an audit event; 32 local tests and six Linux tests passed, including
the actual store/executor continuation without any new queue or publish call.

Applied plan: `80c95ee33b09bdd82d0a14445eed666c558f3baf94317445441c058a2ba6772b`.
Backup: `/mnt/data-disk/pipeline-dispatch/backlog-20261103/x-orphan-recovery-backups/x-orphan-80c95ee33b09bdd8-8bbf4e32188a4b4b867f728cb5274262.sqlite3`.
The first fresh-service guard attempt expired without a task mutation; a new
fresh idle window was captured and the second audited attempt succeeded.
At 14:37 the normal planner confirmed the original queue and reservation. By
14:39 the collector had restored that child to queued with no hold, using its
already committed November 2 parent authorization. A one-scan cutover-paused
transition cleared automatically; no duplicate disposition or queue was made.

Final 14:39 snapshot: configuration70, actual CPU4/6 and GPU6/4 live pools,
CPU4 + GPU6 preparations executing. Of the six existing-artifact publications,
5392/5394/5398 had unique real Post IDs at attempt1/unknown0; 5396/5458 remained
in publication; 5395 had a platform error at attempt1/unknown0 and was not
automatically resent. Remaining normal business work continues asynchronously.
All code was GitHub-first and all changes preserved running node supervisors.
Skill context was unchanged: requested numeric capacities are operational
configuration, and the reusable recovery behavior is documented in this repo.

The public wording change was pushed as `f0341701d4f648b447b8a4990e93b2fc1d31a579`
(static implementation `8977052`) and installed at 14:14 in both main runtime and
Nginx static directories. Public HTTP 200 and exact GitHub file hashes passed;
no service restarted. Backup:
`/mnt/data-disk/pipeline-dispatch/backlog-20261103/static-queue-wording-20261103T061441Z`.
The label now says queued work and explicitly includes planning, future
appointments and account waits. Counts and permissions are unchanged.

## Rollback boundary

To restore prior capacities, submit the four saved ceilings/modes and global
limit from `before.json` through the same administrator capacity API with the
current expected version. Lower targets drain existing work naturally. Do not
restore the old database or overwrite receipts, task dispositions or account
fences. This change adds no new service or operational script dependency.
