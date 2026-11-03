# X template save timeout repair

Saving the 86-account template repeatedly fetched all 103 account projections.
One live list read took 3.378 seconds; the main API abandoned the save after its
120-second timeout (HTTP 503). The editor kept waiting without a client deadline.

`XAutoPostService._validate_accounts` now reads one request-local account list,
indexes it by account ID and validates every selected account. Missing/duplicate
identities, publish eligibility and canonical language checks remain. Execution
and final publication still perform their existing current-account checks.

The editor aborts a save request after 30 seconds, restores the save button and
keeps the draft. Its message describes an unconfirmed result and asks the operator
to check the list before another save. It never retries automatically. A confirmed
save still returns to the list. The page script cache version is updated.

## Validation

```
python -m unittest scripts.test_x_auto_post_service scripts.test_x_auto_post_validation scripts.test_x_auto_post_x_sidecar -q
node scripts/test_x_auto_duration_ui.js
node --check static/x-auto-publish-template.js
git diff --check
```

Tests cover a real 86-account create/update store path with one list request each,
identity/eligibility/language rejection, no token verification or publication,
successful editor redirect, bounded timeout, retained inputs and ordinary errors.

## Scoped deployment and rollback

Push the reviewed commit first. Run `scripts/deploy_x_template_save.py --commit
<full SHA>` on 43.166.187.96. It fetches that SHA from GitHub, checks the exact
deployed baselines, rehearses the actual template config against an isolated
database, backs up both live SQLite databases and all changed files, and pauses
only active Auto triggers until existing workers finish. It overlays only
`features/x_auto_posts/service.py` in the current runtime and the editor JS/HTML
in runtime/main/Nginx. It restarts only `x-auto-post-service.service`, checks
health and unchanged main/X-sidecar PIDs, repeats the isolated save and restores
the original trigger states. It does not update a production template or publish.

Rollback with `python3 <staged-script> --rollback <backup>` from the deployment
manifest. This drains Auto workers and restores only these files, restarting the
Auto service. Never restore the database backups or token history for code rollback.
