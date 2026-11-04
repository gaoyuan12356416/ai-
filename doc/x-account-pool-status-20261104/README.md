# X account material-pool status

The global X account list shows each account's material-pool plan status between
the backend authorization status and row actions. Enabled plans are green;
paused plans are amber. A paused plan with `unknown_outcome` is red with
`结果待核对`. Loading and failed reads are explicit and never imply a paused plan.

Reuse the same account-scoped pool endpoint as the existing dialog, requesting
only one item. Load rows near the viewport with up to three concurrent reads,
and reuse results while filtering. Refreshing the list reloads status. Successful
dialog reads, including after enable/pause, update the matching row immediately.
Ignore older list generations and older pool versions to avoid stale overwrites.
The existing API handles permissions and outcome/language synchronization.
This change adds no API, schema, scheduler, or publishing rule.

Validation: inline and shared JavaScript syntax checks; `git diff --check`;
16 browser smoke assertions on synthetic accounts, including colors, failure and
missing data, default paused state, filtering/empty rows, real dialog callbacks,
out-of-order reads, scrolling and narrower screens. With 50 fixture accounts,
the initial viewport requested seven pools with a maximum concurrency of three.

Deploy only `static/x-account-list.html` and
`static/x-video-account-settings.js` from the exact pushed GitHub commit to
the main static root, Nginx static root, and resolved X runtime static root.
The shared script cache version is `20261104pool-status`. Preserve the already
deployed pool-admission recovery fix and account-language filter.

Before deployment verify the live SHA-256 baselines:

- HTML: `634fef06bebe72f0f8dd8779d545cd1adb11dcac756f932b67fa3b3f8a4fade0`
- JS: `a5505f941fed0c6ba0b5acbb0c07b68ebb928b8f3fdb4acab7643dac9ea6148e`

Record file hashes, resolved destinations, ownership/modes and service PIDs in
`/mnt/data-disk/x-account-pool-status/<UTC timestamp>/deploy.json` and back up
all six files there. Install each file atomically, then verify public HTTPS
content against the Git blobs and unchanged service PIDs. No restart or reload.

Rollback: run `python3 <backup directory>/rollback.py`. The scoped script verifies
that the files still belong to this release before restoring the saved static
files. It does not touch databases, credentials, queues, timers or services.
