# New App USD balance alert

Scope: only the developer billing account associated with the new test App.
This is not a balance for each authorized X profile. The existing production
App and publishers are not modified.

Official endpoint: <https://docs.x.com/x-api/usage/get-usage-credits>.
GET `https://api.x.com/2/usage/credits` with the new App Bearer Token returned
HTTP 200 on 2026-10-19: total USD 9.97, prepaid USD 5.00, free USD 4.97.
The monitor uses `total_balance`, including free credit, and the strict test
`total_balance < 5`. Missing data, partial errors, NaN, invalid amounts and
failed requests are unknown, never zero.

## Execution and delivery

- `scripts/x_app_credit_monitor.py`, stdlib only, runs on CPU 43.166.187.96.
- Read existing private `data/credentials.json`; no X user-token refresh,
  publisher DB access, Post creation or production service restarts.
- State and private notification configuration are in
  `/mnt/data-disk/x-new-app-sandbox-20261019/data/credit-monitor/`, mode 0700/0600.
- Feishu private recipient is the existing validated 郜远 `open_id`. Never
  substitute a group chat. Credentials come from existing server configuration.
- Use the secret-free transport excerpt of the personal `feishu-bot` skill.
- The chat heartbeat schedules one `--notify` invocation hourly through the
  saved CPU SSH helper. It requires the Codex host to be available; there is no
  server systemd timer or cron. Do not add a duplicate scheduler.
- A low-balance episode sends once. A successful later read at USD 5 or higher
  rearms the next episode. Locking, persisted UUID and pre-send durable intent
  prevent overlapping/repeated sends. A timeout/unknown message result is held
  for reconciliation; it must not be blindly resent.
- Failed balance reads preserve the last successful balance. The heartbeat
  surfaces three consecutive query failures or any delivery failure rather than
  pretending monitoring is healthy. Routine healthy checks remain quiet.
- `--send-setup` sends one configuration confirmation with the actual balance;
  if the actual balance is low, it sends a real low-balance alert instead.

## Validation and rollback

Run `python -m unittest discover -s scripts -p test_x_app_credit_monitor.py`.
Tests cover exact threshold, duplicate suppression, recharge/rearm, invalid or
partial amounts, negative prepaid balance, crash/timeout, setup message and
private-recipient enforcement. Run the exact release on CPU and read the saved
Feishu message receipt. A second run must not send the setup message again.

To stop, pause the named chat heartbeat `X 新 App 余额低于 5 美元提醒` with the
Codex automation tool. Keep `state.json` and its receipts; deleting them could
resend the same low-balance notification. No production rollback or restart is
required. Code releases are immutable under the existing sandbox release root.
