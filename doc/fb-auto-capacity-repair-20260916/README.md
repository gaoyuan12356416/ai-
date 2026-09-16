# FB automatic publishing capacity repair

The Page pool grew beyond the configured 20-job batch limit. The planner correctly
rejected the expanded pool with `fb_auto_capacity_exceeded`, then the due slots
expired without creating runs or making Graph publishing requests. Healthy timers
and an empty execution queue did not indicate successful scheduling.

Set `FB_AUTO_MAX_JOBS_PER_SLOT=100` in the CPU environment, using the reviewed value
in `deploy/fb-auto-post.env.example`. The initial repair used 25; the operator then
explicitly requested 100 and manually re-enabled the template. The observed pool needs 23 jobs per batch and
115 jobs per day. The daily limit remains 500; Page eligibility, template overlap,
material selection, expiry and duplicate-publication checks remain in force.
The GPU preparation worker count is still 1 and the Graph worker count is still 4.

Deploy only this non-secret environment value. Preserve the running source release
and any previously deployed scheduling audit additions; do not replace the full
release from this branch. Do not copy the example's credentials or closed gates
over production. Back up `/etc/fb-auto-post.env`, take an online operational SQLite
backup under the verified data disk, drain in-flight work, then restart only
`fb-auto-post-service.service`. For an enabled template, temporarily stop the five
scheduler/plan/prepare/execute/reconcile timers. Stop the bounded planner client
to prevent it from claiming another slot; let any request already executing in
the sidecar finish and settle its due-slot lease. Wait for zero active planning,
GPU-preparation and Graph-submission leases before restarting the sidecar. Always
restore the original timer states, including after a failed or timed-out drain.
Pending future tasks and the enabled template must remain intact.

Verify both limits with the real read-only Page pool and current metric cache:
20 rejects the current pool; the reviewed limit passes the full activation validation. Read the
effective service environment after restart, check both CPU/GPU health endpoints,
and verify all seven timers are active. Compare template versions, due slots,
runs, tasks, attempts and publish-ledger rows before/after maintenance. Do not
re-enable a disabled template or replay expired historical slots as a diagnostic.

Rollback changes only the environment value to its backed-up value, then restarts
`fb-auto-post-service.service`. Never restore older SQLite files over publication
facts. An operator-approved template reactivation must use its normal versioned
enable path and future schedule; it must not backfill missed historical slots.
