# YouTube cover queue isolation, 2026-11-17

12 preparations created 17:31–18:31 CST stayed queued. Dispatch had all four
cpu-main preparation slots occupied by long FB render jobs. GPU explicitly
excludes YouTube preparation; the cloud video renderer cannot generate covers.
The image sidecar was healthy. This was admission starvation before generation.

Deploy `python3 ops/youtube-cover-lane/deploy.py` once, after pushing this commit.
It enrolls an audited `cpu-youtube-cover` identity with existing authority APIs,
preserves capacity-before evidence and fingerprints a separate copy of the
existing node runtime. Only current youtube_auto_publish modules are overlaid
to retain current reference/provenance validation. Secrets remain private 0600
files, outside Git. A single existing process_worker runs preparation only;
publication capacity is explicitly zero. Original cpu-main, GPU, coordinator,
canonical API and upload processes are neither replaced nor restarted.

Existing reservations, generation versions, paid-result receipts, immutable
reference images, source checks and approval requirements stay authoritative.
No queued task is recreated or reset; unknown outcomes are never replayed.
Concurrency of this dedicated lane is one, preventing simultaneous paid cover
attempts on the new lane. Existing CPU capabilities are unchanged.

Validation: three scope tests (including rejection of publisher/FB adapters),
syntax and diff checks; deployed runtime must additionally import successfully
as pipeline-worker and advertise exactly the three cover capabilities. Inspect
original task IDs in both canonical and dispatch databases for actual progress.

Rollback: `systemctl disable youtube-cover-lane.service` then
`systemctl stop youtube-cover-lane.service`. Stop waits for the current claim;
never force-kill paid generation. No shared DB snapshot should be restored.
Retain the inactive enrollment/audit and runtime evidence. To undo installation,
remove its unit only after stopped, then systemctl daemon-reload.

The independently failing youtube-cover-material-sync unit reports
unexpected_cos_destination. That material-library sync does not own these queued
preparation claims; its recovery is outside this admission fix.
