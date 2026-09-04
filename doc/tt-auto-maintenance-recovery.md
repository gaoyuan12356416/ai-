# TT automatic publishing maintenance and recovery

## Incident and correction

On 2026-08-31 18:53:52 Asia/Shanghai the GPU migration controller paused
seven TT timer/path units. The journal remained `restored=false`; the API
process stayed healthy while scheduled publishing stopped. API process
availability alone was therefore insufficient evidence of recovery.

Production `/health` now checks the three automatic trigger units, failed
scheduler/runner services, and scheduler heartbeat (180 seconds). It returns
HTTP 503 and `automation.problems` while publishing automation is unavailable.
The independent healthcheck timer remains enabled during maintenance and
records readiness failures in systemd/journal. It sends no external messages,
does not publish, and never starts or resumes a publishing unit. A legitimate
maintenance pause is also not operationally ready; do not suppress this state
or use a timed auto-resume to override maintenance ownership.

## Required maintenance closeout

1. Record the owner, reason, expected end time, prior unit states and live task
   counts before pausing. Keep the readiness healthcheck outside the pause set.
2. Inspect the pause journal at closeout. A paused/unrestored TT journal is an
   incomplete closeout even if GPU and API process health are green.
3. Recheck GPU routes, accounts, prepared assets and publish evidence. Preserve
   task IDs, captions, material reservations and all published/unknown outcomes.
   An overdue ready task may publish as soon as the existing runner resumes.
4. Back up the live SQLite databases using the online backup API and save the
   maintenance journal and unit states. Use the existing migration controller's
   `resume tt --apply` to restore the original seven triggers and journal;
   do not merely change `restored` or run an unaudited list of starts.
5. Wait for a natural scheduler tick, require `/health` HTTP 200,
   `automation.ready=true`, journal `restored=true`, and successful independent
   healthcheck. Reconcile actual published task IDs, not just running processes.
6. Check missed dates separately. Scheduler grace windows do not imply all
   missed days will be recreated. Do not backfill runs without explicit scope.

## Deployment and rollback

Deploy the exact GitHub revision based on production `9425b39`; preserve all
live databases and secrets. Install the healthcheck service/timer from `deploy/`
and enable the timer. Restart only the TT auto API while triggers are paused
and all runner claims are drained. Before resume, the new health endpoint must
report the existing stopped trigger state as unhealthy.

For code rollback, pause triggers through the audited maintenance controller,
drain ongoing work, switch `/opt/tt-auto-post/current` to its saved target,
restart only `tt-auto-post-service.service`, and disable the new healthcheck
timer if the old health contract lacks `automation`. Restore original trigger
states after validation. Never restore a pre-publish SQLite over later facts.
