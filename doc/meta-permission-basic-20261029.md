# 803 to 916: basic advertising permission follow-up

The operator changed the target from complete asset management to basic advertising on 2026-10-29. Only the exact 6,756 assets in the supplied asset-detail.csv are in scope: 3,213 accounts and 3,543 Pages. Input SHA256: `cd2303e92ad3e12f7517b923d5fe89bafce862ed4d57a666b3e7476c1d90c766`.

## Permission and state policy

- `basic_ads` requires ADVERTISE and ANALYZE for accounts and legacy Pages, or PROFILE_PLUS_ADVERTISE and PROFILE_PLUS_ANALYZE for new Page assignments using that shared model. Existing PROFILE_PLUS_FULL_CONTROL satisfies the requirement. Preserve all current tasks; do not add MANAGE, FULL_CONTROL, messaging, moderation, leads or revenue permissions.
- Already usable assignments are excluded without requiring delegation authority. Assignment plus actual token reads must agree; inaccessible or conflicting assignments remain unknown.
- Account status must remain active in Meta and normal/non-inactive in the backend. Page status must be normal/non-forbidden in the backend and published/not permanently closed in Meta. Unknown and abnormal assets never enter the write queue.
- Sharing only needs to cover the requested task bundle. A live BM ADMIN with basic account access can submit assignment of those shared tasks; MANAGE is not required by this local preflight. The API is authoritative if it rejects that operation.
- For missing accounts, target account-side GET is expected to be denied. Absence instead requires a completely paged own assigned_ad_accounts edge, cached for at most 60 seconds, alongside a fresh administrator account-side assignment read. Read errors are never interpreted as absence.

## Execution and recovery

Use a new format-3 immutable manifest containing the permission profile and input hash. Never edit the old full-control manifests or journals. Link recovery to the last paused run and use the same credential fingerprint, Page and target identity for a single recovery canary with the new requested tasks. If that Page is now already sufficient, do not submit a redundant grant and do not infer recovery from read access.

368/1390008 and rate limits pause the BM without switching credentials. Unknown writes get readback only. Continue normal administrator rotation only after an acknowledged recovery grant and independent administrator plus target readback. Intervals remain at least 30 seconds globally and 300 seconds per token. Revalidate identity, status, assignment and current sharing before each write. No ad, campaign, BM membership or application database is modified.

## Runbook

Run audit with `scripts/meta_asset_permission_followup.py --permission-profile basic_ads --scope-csv INPUT --prior-run PRIOR_PAUSED_RUN --run-dir NEW_RUN --code-version COMMIT`. Inspect the frozen counts and hash, then use `scripts/meta_asset_permission_sync.py execute --run-dir NEW_RUN --code-version COMMIT`. Resume only the same unpaused ledger.

CPU code path: `43.166.187.96:/mnt/data-disk/meta-permission-sync/code-20261028`. Previous code/rollback point: `7a08b72a9690dbc8ab567e122d6b9ce1308e68b9`. After the process is stopped, code rollback is `git -C /mnt/data-disk/meta-permission-sync/code-20261028 checkout --detach 7a08b72a9690dbc8ab567e122d6b9ce1308e68b9`. This does not revoke grants. Permission rollback must restore only the task delta from the exact journal and pre-write snapshots, retaining subsequent unrelated changes.

Validation: `python -m unittest discover -s scripts -p 'test_meta_asset_permission*.py'`. Tests cover both task models, preserving existing permissions, explicit CSV scope, exclusion of existing access, normal/unknown states, incomplete reads, missing-account checks, partial BM grants, live identity, rotation, 368 pause, recovery and resume.

No backend service restart is required. Shared backend skill context remains unchanged; this is a scoped one-shot operational profile documented here.

API task enums and assignment endpoints: [Meta AdAccount SDK](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/adobjects/adaccount.py), [Meta Page SDK](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/adobjects/page.py), [Meta SystemUser SDK](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/adobjects/systemuser.py).
