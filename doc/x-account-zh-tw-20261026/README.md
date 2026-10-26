# X account Traditional Chinese language option

The account list previously offered `中文 (zh)`, which did not match catalog
dramas tagged `zh-TW`. The selectable Chinese option is now
`繁体中文 (zh-TW)`. The submitted and stored value remains canonical `zh-tw`,
which the existing backend compares equal to catalog `zh-TW`.

Option lookup and selection normalize both sides, so a persisted `zh-tw` or
`zh-TW` produces one selected, correctly labelled option after refresh.
Other languages retain their existing values. Existing saved `zh` values remain
visible as legacy values until an operator explicitly changes the account.
No account migration, task rewrite, history replay, or publisher restart is part
of this static release.

## Validation

- Eight existing `scripts.test_x_account_language_routing` tests passed.
- An isolated temporary account passed the real `set_account_drama_language`
  save and readback with `zh-TW`, persisted as `zh-tw`.
- Canonical language comparison matches catalog `zh-TW` and excludes `zh` and
  `zh-CN`.
- Playwright loaded the actual page using a simulated account/API: the option
  label was `繁体中文 (zh-TW)`, selection submitted
  `{"drama_language":"zh-tw"}`, and the subsequent account reload selected the
  same option without duplication. This did not change a production account.
- `git diff --check` passed. Public and service-file hashes must match the
  committed HTML before deployment is accepted.

## Deployment contract

CPU host: `43.166.187.96`.
Only `static/x-account-list.html` is deployed, from the exact GitHub commit, to:

1. `/root/drama_material_service/static/x-account-list.html`
2. `/opt/x-post-automation/current/static/x-account-list.html`
3. `/usr/share/nginx/html/x-account-list.html`

All three pre-deploy files must have SHA256
`425a08d75b73a3e4942b192eb31ef07131ce7449a819243d67690f31eb65d1f3`.
Abort if any differs. Back up each target and its mode, record service PIDs,
replace each file atomically, verify installed/public hashes, and verify service
PIDs are unchanged. The data disk must have the expected mounted UUID.

Rollback restores only these three saved files with an expected-current-hash
guard. Do not restore any database, tokens, tasks, schedules, or ledgers.
The backup manifest records the resolved runtime target, exact hashes,
implementation commit, and rollback command.

No publishing-contract skill rule changes are required; this is a correction
to the static options using the already established exact-language contract.
