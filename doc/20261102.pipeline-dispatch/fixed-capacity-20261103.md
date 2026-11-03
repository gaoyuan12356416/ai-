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

## Rollback boundary

To restore prior capacities, submit the four saved ceilings/modes and global
limit from `before.json` through the same administrator capacity API with the
current expected version. Lower targets drain existing work naturally. Do not
restore the old database or overwrite receipts, task dispositions or account
fences. This change adds no new service or operational script dependency.
