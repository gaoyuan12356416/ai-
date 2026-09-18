# FB Page capacity and scheduling repair

The experiment pool expanded from 23 to 145 Pages. Five daily slots require 725
independent videos and Posts. Existing runs contain only the original 23 Pages;
changing limits alone does not expand those immutable snapshots.

The production publisher claimed only four tasks every two minutes. Its ten-minute
late cutoff therefore skipped the last three Pages even in a 23-Page run. The
publisher now refills eight bounded lanes, and the reconciler four, until idle or a time budget.
Preparation refills two lanes, ordered by the existing publish deadline. Every
runner finishes in-flight requests before exiting; systemd timeouts exceed both
the claim budget and the longest in-flight request.

Ten production batches of four claims took 22.93–57.72 seconds on September 18.
The expanded pool therefore uses a 30-minute automatic claim window, a 30-minute
publisher drain budget and a 60-minute service timeout. The shared Graph request
interval remains 0.5 seconds. Already skipped tasks stay terminal. Calendar
prebuilding reaches the day after tomorrow, always ordered by publish deadline.

Capacity defaults are 200 jobs per slot and 1000 per day. The GPU preparation-only
worker supports at most two jobs with per-job serialization, a shared cleanup
lock, active-directory protection and 32 GiB free-disk reserve. The recommended
8-vCPU-host drop-in caps FB at four CPU cores and 8 GiB RAM. The existing OpenCL,
RGBA cache, random recipe, video/audio settings and COS identity checks are kept.

Deploy CPU runner/units and environment independently from the GPU worker. The
source composite preserves the current CPU due-target audit table and the
already-deployed GPU RGBA compositor. Back up exact files, effective environments
and online SQLite; drain planning/preparation/publication before restarting.
Restart the FB reverse tunnel if its GPU worker restart deactivates it. Never
restore a prior database over live publication facts.

Run `python -m unittest discover -s scripts -p 'test_fb*.py' -q` and the random-GPU
tests. `scripts/benchmark_fb_prepare_capacity.py` compares two existing fixed
recipes sequentially and concurrently without Graph APIs. Require unchanged
output fingerprints before enabling concurrent production rendering.

The Page expansion needs a separately audited amendment to the six future runs,
preserving all existing tasks, media, attempts, ledgers and historical skips.
No expired slot is replayed. Language routing is the operator's subsequent change.
