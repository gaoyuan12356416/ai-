# Selected account pools to system user 916

The operator explicitly requested a new authorization attempt for the live accounts in screenshot-selected pools 567 (DRAMAWAVE narku 2290 automatic bidding) and 484 (re-targeting 2290 core audience). This does not resume the prior broad asset or Page run.

Read-only database mapping is `ads_alternative_account_group` to `ads_alternative_accounts.group_id`. Include platform 0, deleted_at 0 membership only; deduplicate account IDs while retaining both pool links. Require pool account_status 1, backend account_status 1/is_inactive 0 and Meta account_status 1. A member removed from both selected pools before execution is skipped.

The initial live read found 39 + 60 memberships, 98 unique surviving accounts, with one account shared by the pools. 75 unique accounts already satisfy ADVERTISE and ANALYZE; 23 require authorization (10 in pool 567, 13 in pool 484). Historical removed membership is retained in evidence but never executed.

`scripts/meta_account_pool_permission_sync.py` consumes fresh read-only evidence (at most 30 minutes old), checks current membership, identities, assigned users and BM shared tasks, and freezes an account-only basic_ads manifest. The executor rechecks state, membership, target tasks and sharing immediately before each write.

The first account is an explicitly requested scoped probe using the same credential fingerprint as the previously blocked Page operation. A successful account probe only opens this new account manifest after acknowledged write plus administrator and target readback. It does not establish that Page authorization is unrestricted. A 368/1390008 response pauses this manifest immediately, without trying a different account or token. Other errors retain the existing unknown-write/readback and pause rules. Old paused manifests, operator pause records and Page assignments remain untouched.

Code is deployed from GitHub to `43.166.187.96:/mnt/data-disk/meta-permission-sync/code-20261028`; rollback point `ea8cf7635f7b9ec410c67ab59a12a595f8507eac`. Once this scoped process is stopped, code rollback is `git -C /mnt/data-disk/meta-permission-sync/code-20261028 checkout --detach ea8cf7635f7b9ec410c67ab59a12a595f8507eac`. Code rollback never revokes grants; task rollback requires current readback and the exact pre-write journal snapshot.

Run tests with `python -m unittest discover -s scripts -p 'test_meta_asset_permission*.py'`. No service restart, cron, application DB mutation or shared backend skill change is involved.
