# X CDN allowlist and explicit preflight recovery

The SocialKit source catalog can return HTTPS video URLs hosted on
`socialkit-cdn.yingliang.tech`. X's download preflight and publisher must both
allow this exact hostname. Keep all existing hosts and HTTPS, redirect, media,
size, account, language, and duplicate checks.

Configuration-only rollout updates the exact host lists in
`/etc/x-post-schedule.env`, `/etc/x-post-daily.env`, and
`/etc/x-post-automation.env`. Secrets and production environment files stay off
GitHub. Back up these files and both SQLite ledgers to the mounted data disk
before mutation. Drain publishers, restart only the X and X Auto sidecars, and
verify their loaded host lists and health before resuming publishers. The
loopback health URLs are `http://127.0.0.1:8810/health` and
`http://127.0.0.1:18833/health`; the public OAuth prefix is not the loopback
health route.

The operator recovery script is deliberately restricted to the two known
preflight failures. It requires unchanged failed tasks, zero publish attempts,
zero unknown outcomes, empty canonical queues, and failed-preflight execution
envelopes. It creates one idempotent manual child run with the original frozen
template version and only the affected accounts. Original tasks, successful
posts, templates, and schedule plans are preserved. Execute pending child tasks
with the existing X Auto runner and retain its shared publication lock.

Run the default read-only check first. Rehearse `--apply --rehearsal` against
online SQLite backup copies, verify idempotency and unknown-outcome rejection,
then apply as `x-post-daily` with the existing service environment. This script
only creates pending tasks; its success does not prove publication. Acceptance
requires both child tasks and their canonical queue/logs to be published with
matching X post IDs and `unknown_outcome=0`.

Production-copy rehearsal passed scoped child creation, repeat-operation
idempotency, original-task preservation, and rejection after an injected
unknown-outcome flag. Rehearsal does not issue X Post or Repost requests.

Rollback restores only the backed-up environment files, drains and restarts the
same sidecars, and restores original timer states. Never restore old databases
or token files after new publishing or authorization facts have been recorded.
