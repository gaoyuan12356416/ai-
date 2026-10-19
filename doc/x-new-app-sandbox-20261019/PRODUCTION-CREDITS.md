# Production X API balance monitor

The user corrected the earlier scope: monitor the X App used by the **running
AI backend**, with a CPU server scheduler, not the new sandbox App or a Codex
automation. The erroneous heartbeat `x-app-5` was deleted on 2026-10-19. Its
private test receipt is retained as historical evidence only.

## Verified source and balance

Read the actual process environment of `x-post-automation.service`, listening
on 8810. Confirm the loaded OAuth Client ID differs from the sandbox's ID, and
pin its SHA256 in the private monitor configuration. Read the production
`/var/lib/x-post-automation/accounts.sqlite3` in read-only/query-only mode and
select an active, publishing-approved account with the freshest user token.
Use that token for GET `https://api.x.com/2/usage/credits`; no token is printed
or copied. Actual HTTP 200 on 2026-10-19 returned USD 85.09 total, 85.09 prepaid,
0.00 free. This is the shared developer payer balance, not an X user's wallet.

If no selected token remains fresh, delegate one bounded verification to the
existing authenticated loopback `/internal/accounts/{id}/verify`, with
`only_refresh_required` and `preserve_transient_status`. This retains the running
service's per-account lock and token lifecycle. The monitor itself never writes
publisher DB/token files, refreshes tokens independently, or changes approvals.
The existing verification operation may rotate an expired token and update its
profile/status; this is normal authorization maintenance. If it fails, do not
cycle through accounts. Failed queries remain unknown, never zero. A loaded
App change requires a deliberate source revalidation and fingerprint rebind;
never silently query old user tokens against a newly configured App identity.

## Server scheduling and notification

- CPU: `43.166.187.96`; independent root
  `/mnt/data-disk/x-api-credit-monitor`.
- Code: immutable `releases/<commit>/`, `current` symlink.
- Private `config.json`: expected running Client ID hash and existing Feishu
  private recipient configuration for 郜远. Mode 0600; no secrets in Git.
- State: `data/state.json`, exclusive lock and durable pre-send UUID/receipt.
- `x-api-credit-monitor.timer`: every hour at minute 00, up to one minute
  scheduling jitter, persistent catch-up after downtime.
- `x-api-credit-monitor.service`: one-shot `x_live_credit_monitor.py --notify`.
  Root is required to read the existing process environment and private token
  files. Systemd makes production data read-only for this process and permits
  writes only to the monitor state directory. No main/X publisher restart.
- Strict threshold: total available USD `< 5`; equality does not alert.
  One Feishu alert per low-balance episode; recovery to >=5 rearms it.
- Three consecutive query failures send one separate monitor-failure alert;
  App identity change alerts immediately. Unknown delivery stays held; no blind
  retry. No Posts are published/deleted and no publishing schedule is changed.
- The scheduler runs entirely on CPU. It does not require desktop/Codex uptime.

## Validation and rollback

Run both `test_x_app_credit_monitor.py` and `test_x_live_credit_monitor.py`.
Verify the exact GitHub checkout, `systemd-analyze verify`, actual unit run,
balance source, private Feishu message readback, timer next-run, and unchanged
main/X service PIDs. Preserve before-deployment metadata in `backups/`.

Stop checks with:

```sh
systemctl disable --now x-api-credit-monitor.timer
systemctl stop x-api-credit-monitor.service
```

Keep the monitor's state/receipts. No production rollback is necessary because
the production App/code/configuration is not replaced. Do not restore the
deleted Codex heartbeat or install a second scheduler.

## Acceptance, 2026-10-19 11:55 Beijing

- Deployed GitHub code `7fe96d877f6494dcfa80e1e23f19d839dcbea56a` to the CPU
  release path above. Sixteen tests passed on Windows and CPU.
- `systemd-analyze verify` passed; the actual sandboxed systemd service returned
  `Result=success`, `ExecMainStatus=0`. Timer enabled/active, next scheduled run
  was 2026-10-19 12:00:16 Beijing.
- Real production lookup returned USD 85.09, through source service PID 1171982,
  with a current user token; no token refresh was required in this validation.
- Corrected production setup notification was sent once. Feishu GET message
  readback returned code 0, matching message ID and 85.09 content. Receipt is in
  private state, and a subsequent unit run sent no additional message.
- Existing main/X/test service PID/state and publishing-timer snapshots matched
  before/after exactly. No production App/config replacement or service restart.
- Codex heartbeat deletion confirmed by tool and missing local automation file.
  Skill reference updated to retire the erroneous test-App/heartbeat workflow.
