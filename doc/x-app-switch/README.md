# X application switching and authorization status

Changing the OAuth Client ID does not transfer user grants or their billing
account. Tokens now record a Client ID SHA256 at authorization and refresh.
When the current application differs, the account projects `reauth_required`
and neither verification, refresh nor publishing can use its old token.
Deployment enables strict handling of legacy unbound tokens after tagging them
with the previously running Client ID. Existing disabled accounts stay disabled.

Both account pages show the current API name and distinguish 已授权 from
待重新授权. The personal page supplies a login action; the global list can filter
pending accounts. A completed OAuth callback for the same owner and X user
updates the same row, retaining its ID, owner, language, publish approval,
configured schedules and all publication history. A different owner cannot
take over an account.

The production balance monitor reads the running service's application and
checks its configured Client ID hash. Its optional app-only billing bearer
supports checking credits while all user accounts await authorization. A
configured threshold of USD 10 alerts only below 10, once per low-balance
episode, rearming after a successful reading at or above 10. The configured
Feishu chat message contains a real open-ID mention. Failed reads remain
unknown, and ambiguous sends are held without retry.

## Deployment

`scripts/deploy_x_app_switch.py --config /private/config.json` performs a
read-only preflight. Add `--apply` only for the explicitly authorized application
switch. Run from a clean, GitHub-fetched checkout. Private configuration contains:

- The server credential JSON path; old and new Client ID SHA256 values.
- The user-facing `app_name` and exact live-file hash `baseline` mapping.
- `feishu_recipient` with receive_id_type=chat_id, receive_id, mention_open_id
  and recipient_name. Existing bot credentials are retained on the server.

The deployer copies the current live composite and overlays only the reviewed
account module, blocker helper and two HTML pages. It preserves the previously
deployed preflight-count and platform-sharing changes. The credit monitor is
staged separately with its server-only transport helper.

It pauses execution triggers, drains existing workers, holds the shared locks,
backs up databases/tokens/configuration, marks active old grants as pending,
clears obsolete OAuth states and switches the application configuration. It
does not modify any queue, Post ID, material binding, failed attempt or unknown
outcome. OAuth, its dependent Auto service and the main API are restarted; the
previous trigger/pool-scheduler states are restored only after readiness and
ledger-count checks. An unresolved cutover leaves publishers held with a private
manifest for inspection.

## Acceptance and rollback

Verify production Client ID fingerprint, billing balance, real account-list
status counts, public HTML and health endpoints. Have one operator authorize an
existing business account and confirm that its identity/owner/settings remain
unchanged; validate a single new publication via the normal ledger and X
readback before declaring production publishing healthy. Authorization/status
checks alone are not publication acceptance.

The private backup manifest names the prior runtime and monitor releases,
original environment, account database snapshot, token files and timer states.
Never restore database snapshots over current publishing facts. After any new
OAuth grant, never overwrite its token with an old backup. To return to the
previous App, retain the app-binding guard, drain workers, restore only the old
App configuration and rebind the credit monitor. Restore old account statuses
only for rows whose current credential remains bound to the previous App;
new-App credentials need another login. Code-only rollback must retain the
binding guard or fully reconcile credentials before enabling publishing.

## Validation

Focused tests cover cross-App read/refresh/publish rejection before network,
same-account reauthorization preserving IDs/ownership/language/approval, strict
legacy-token handling, disabled-account preservation, balance queries without
authorized users, strict USD 10 threshold/rearming, and real Feishu mention
payload/receipt. Existing OAuth and monitor regression suites also apply.
