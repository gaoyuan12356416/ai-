# X account list management

The account list displays the running production App's USD credit balance from
X `GET /2/usage/credits`. It queries on entry, manual refresh and every minute
while the page is visible. The request uses only the sidecar's configured
`X_BILLING_BEARER_TOKEN`; no user token rotation or alert message is triggered.
Failures display an unknown balance, never zero or an unlabelled old value.
The existing hourly low-credit monitor is independent and unchanged.

Administrators can remove one account after a browser confirmation. Removal
sets additive `deleted_at`, disables the account and clears publish approval
in the same transaction as the audit event. It hides the account from browser
catalogs while retaining the identity in publisher catalogs as unavailable.
Historical publication, revenue, pool ownership and frozen queues are retained.
This does not delete the X profile or posts. Reauthorizing the same X identity
restores its existing row; publishing still requires explicit approval.

The existing account lock protects local publishing/refresh operations. A
transactional queue/log check also protects distributed uploads and unknown
outcomes, including relay accounts. The separate YouTube-share ledger is
attached when initialized. Untouched queued jobs do not prevent removal;
their eventual credential check rejects the disabled account.

The old attention count is explained by concrete authorization states and
supports one-click filtering. Operating statistics are identified as scheduled
Post/Repost/revenue snapshots, with Beijing update time and yesterday's date.
The two unallocated-revenue cards are removed from this page only.

## Validation

- `python -m unittest scripts.test_x_account_list_management.AccountManagementTests scripts.test_x_account_list_management.CreditsTests scripts.test_x_account_list_routes -v`
- `python scripts/test_x_app_switch.py`
- `python -m unittest scripts.test_x_account_list_deployment -v` covers the
  actual main readiness route and apply/rollback with systemd readiness probes.
- Compile modified Python, check inline JavaScript and `git diff --check`.
- Browser: nonempty rows, attention filtering, balance refresh, deletion cancel
  and confirmation, updated counts, unavailable balance and nonadmin controls.
- No real account deletion or X publication is used for deployment proof.

## Deployment and rollback

Push first; fetch the exact commit into an isolated server worktree. Run
`python3 scripts/deploy_x_account_list.py --prepare` there. The deployment
validates the exact live file hashes, overlays the current composite release
and backs up main/public files. It preserves unrelated deployed changes.

Run `python3 scripts/deploy_x_account_list.py --apply <returned-backup-folder>`.
It temporarily stops only X claims and previously active X triggers, waits for
accepted and distributed work to finish, then backs up live SQLite and tokens.
It switches the sidecar, updates the main API and both static copies, verifies
health and restores prior X admissions. Other platform channels are untouched.
If the drain times out, admissions are restored without a code cutover.
The default drain limit is one hour; use `--drain-timeout-seconds` to set an
explicit limit. Remove the temporary new-connection fence before systemd
startup because ExecStartPost probes the same loopback health port.

For code rollback use the same fetched source:
`python3 scripts/deploy_x_account_list.py --rollback <backup-folder>`.
It drains X again and restores code/static files only. Never restore the saved
SQLite/token snapshots over current state. The additive column and account
disables remain; older UI code may show removed accounts as disabled. After
another release, compare manifests and reconcile changes before rollback.

Official balance contract: https://docs.x.com/x-api/usage/get-usage-credits
