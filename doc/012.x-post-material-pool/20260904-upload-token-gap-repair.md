# Upload credentials and audited gap recovery

Long uploads previously retained the Access Token captured before downloading
and uploading the video. They now recheck renewable authorization before each
media request and before creating the Post. The account lock remains held over
the publish operation, using same-thread reentrancy for the normal refresh path.
Other threads still cannot disable or refresh that account during the write.
Each refreshed credential retains identity, approval, language and Premium gates.
No Post or Repost request receives an automatic retry.

An HTTP 401 now identifies its operation instead of claiming that every case
requires login. An upload can fail with a recently renewed token, so expiry must
not be assumed without timing evidence.

`scripts/x_post_gap_recovery.py` contains explicit operator actions, read-only by
default. Execute them only after backup, quiescence and the shared runner lock:

- Reconcile an unresolved direct material Post using authenticated X evidence
  matching the author, complete expanded body, media identity and attempt window.
  Retain the original actual publication time and all attempt counters.
- Rearm one known Token failure with no media-completion ID, Post ID or ambiguous
  outcome. Preserve queue/material/relay bindings, body, links and attempt counts.
  A later publish goes through the running Sidecar and full frozen-media checks.
- Rearm a same-day zero-queue drama assignment transaction rejection after the
  language-map fix. Preserve its frozen slot, date, scope and body. The audit
  retains the old attempt marker before clearing it for exactly one new plan.
- Compensate a completed same-day material batch only for its frozen accounts
  that have no queue. Use one separately audited child per original batch, with
  normal pool selection and full current preflight; preserve the parent and its
  original daily plan. Failed or ambiguous existing queues cannot enter this
  missing-account scope. Do not collide with any frozen schedule time.

All actions have unique audit identities and reject repeat recovery. Unknown
outcomes are never converted into retryable failures. Historical skipped slots
remain historical gaps; these actions do not synthesize multi-day catch-up runs.

Validation: offline publishing/account/schedule/relay regression suite plus a
private SQLite snapshot rehearsal of evidence rejection, repeated-action fences,
counter and payload preservation, and run aggregation. Deployment must retain
the previous release, database backup and existing account token ownership.
Rollback restores code only, never publication or token history.
