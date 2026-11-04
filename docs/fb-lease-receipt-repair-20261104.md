# FB lease, receipt and CPU preparation repair — 2026-11-04

An expired FB preparation lease could leave FFmpeg rendering into the same file
as a successor. Late successes then remained unresolved after a newer failure.

The repair checks leases before and during render, kills/reaps expired renders,
serializes each FB job with a persistent OS file lock across local processes,
and delays coordinator reclamation by 30 seconds for cleanup. Authenticated
obsolete failure receipts are acknowledged without changing successors. Frozen
recipe, original source, content-addressed COS journal, and publishing guards
remain authoritative. Output duration is verified against the source.

The bounded recovery CLI defaults to a read-only plan. Apply requires paused FB
admission and the exact plan digest. It checks authenticated old claims, frozen
native identity, recovery window, current template, full artifact evidence, and
absence of publication, platform operations, unknown outcomes or conflicting
page/material history. It restores the original native tasks and old preparation
receipts, preserving attempts, source mappings and shortlinks. It never calls
Graph or creates a publishing attempt. Repeating completed recovery is a no-op.
Evidence, claim secrets and DB snapshots remain outside Git.

Production executor changes from 179596a for X media are retained; they were
already live but absent from the 17a9645 branch baseline. The existing matching
test fixture accepts the existing x_network keyword.

## Validation

103 tests pass: real child cancellation/reaping, cross-process exclusion,
worker/executor/coordinator, preparation recovery and late artifact correction.
Fresh production DB copy rehearsal restored six artifacts without publication,
preserved identity/attempt counts, and proved idempotence. Existing post IDs,
unknown outcomes, changed material IDs and incorrect hashes were rejected.
All six actual COS files match full SHA-256/size, H264 High 720x1280 30fps and
original source duration within 1.5 seconds.

Six concurrent 8-second real-source CPU renders measured:

| Filter threads | Wall seconds | CPU seconds |
|---|---:|---:|
| 4, baseline | 25.239 | 348.477 |
| 1 | 37.508 | 300.141 |
| 2, selected | 24.409 | 315.741 |
| 4, baseline repeat | 24.750 | 352.122 |

All 240 decoded frames match exactly across variants. Selected settings bound
four template decoders to one thread each and filters to two. Encoder threads,
libx264 veryfast/CRF 21, pixels, audio, templates and worker capacity stay fixed.
Measured CPU time falls about 10%; short-trial elapsed improvement is only 2–3%,
not a full-job throughput guarantee. The slower one-thread option was rejected.

## Deployment

Fetch the exact pushed commit from GitHub into a clean source checkout. Run:

```
python -m scripts.deploy_fb_lease_repair prepare --source SOURCE --commit SHA --folder RELEASE --host cpu
# use --host hk on HK
systemctl stop --no-block pipeline-node-cpu-main.service
# HK: systemctl stop --no-block pipeline-node-hk-gpu.service
# Each node switches after its own in-flight work finishes, MainPID=0.
# Coordinator drains only accepted HTTP requests under a bounded gate.
python -m scripts.deploy_fb_lease_repair apply --folder RELEASE
# Apply CPU first, then HK.
```

The helper verifies exact live baselines and unit hashes, preserves unrelated
runtime overlays and ownership, validates imports as the service user, and saves
a private manifest. CPU changes coordinator symlink and node override; apply returns partial while
that node finishes and can be repeated. HK changes
only its node override. Original environments remain. Restore original tasks
using a fresh recovery plan and digest while FB admission is disabled. Verify
outbox ACKs, restore FB admission, then reconcile native and central publication.

## Rollback

Disable FB admission, gracefully stop each affected node and wait for its completion as
above. On CPU, then HK, run:

```
python -m scripts.deploy_fb_lease_repair rollback --folder RELEASE
```

This restores the recorded coordinator symlink/removes its own node override,
then starts original services. Re-enable FB after health checks. The helper
rejects concurrent release drift. Never restore business DB backups after
publishing resumes: keep receipts and ledgers to prevent duplicate publication.

CPU already contains the independent 10500ac two-MiB X segment adjustment.
The deployment overlays only the two FB executor hunks onto each reviewed host
baseline, retaining CPU 10500ac and HK 179596a behavior respectively.
