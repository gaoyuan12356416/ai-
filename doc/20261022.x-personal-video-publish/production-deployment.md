# Production deployment, 2026-10-22

The user authorized production deployment, backups and rollback, with no real X test posts.

## Assembly

`deploy/x-personal-baseline.json` freezes the existing live file hashes. The adjacent
`x-personal-overlay` contains the reviewed merged files for the sidecar composite,
main API, public static root and one new Nginx snippet. The main API merge adds 23
lines and retains the existing video-editing and FB identity integrations. The
release is copied from the live composite so the separate YouTube share worker
and other live-only modules remain available. The old release remains intact.

## Procedure

Fetch the exact pushed commit into `/mnt/data-disk/x-personal-publishing/source`.
Run `python3 scripts/deploy_x_personal_publish.py --stage`. This checks old hashes
and rehearses the additive schema migration twice on an online database backup.
Activate the returned backup directory with `--activate <backup>
--enable-publishing --enable-pool-scheduler` only after reviewing rehearsal results.

The installer first waits for legacy preparation workers while their normal
timers remain active. It then holds only new Auto execution triggers while the
current Auto upload finishes; schedule creation and metrics continue. After
workers become idle it pauses other previously active X triggers, drains any
short race and YouTube share requests, and acquires shared publisher locks. It backs
up code, configuration, units, databases and tokens to the data disk. It starts
main API, X sidecar and X Auto in dependency order, verifies old-column ledger
hashes and token fingerprints, then restores trigger states and enables the new
empty personal pool scheduler. Existing inactive triggers remain inactive.

## Rollback

Use `python3 scripts/deploy_x_personal_publish.py --rollback <backup>`. The command
restores code/configuration and service states, but never overwrites live databases,
tokens, queues or publication history. Once real personal tasks exist, a complete
executor rollback is deliberately refused: close the new entry first and keep the
new executor until nonterminal and unknown outcomes are reconciled.

## Verification boundary

Production checks use static GETs, authenticated catalog/account/record GETs,
read-only database queries and service health. They do not submit a publish batch,
enable a real account pool, or issue an X publishing request. Previously configured
automatic business tasks may continue normally after their triggers are restored.

Local recheck: 106 focused Python tests and 13 frontend cases passed. Production
receipt and final validation results are recorded after activation below.

## Deployment guard correction

The first cutover restored the previous release automatically when the main
user-table invariant detected existing startup normalization: all user timestamps
are refreshed, and two stale avatars were populated from existing sessions.
No personal tasks had been created. Code/configuration and original triggers
were restored; historical databases and tokens were never overwritten.

The corrected guard ignores only user `updated_at` and the expected new
`x_video_publish: true` administrator permission. Identity, status, avatars,
existing permissions and ordinary-user grants remain guarded. Four regression
tests pass on both Windows and production Linux. All publisher ledger and token
checks retain their original strictness.

## Production receipt

Deployment completed on 2026-10-22 at approximately 20:07 Beijing time.

- Deployed GitHub commit: `30f1d0518a753d3ba9d348a5cc5a7b219af425a0`.
- Current release: `/mnt/data-disk/x-post-automation/releases/20261022T120433Z-personal-30f1d0518a`.
- Backup: `/mnt/data-disk/x-post-automation/backups/20261022T120433Z-personal-30f1d0518a`.
- Previous release retained: `/mnt/data-disk/x-post-automation/releases/20261022-account-resume-59651f3`.
- Existing account-resume publisher changes are preserved in the composite.
- Manifest confirms `deployed`, all original triggers restored, unchanged legacy
  publishing ledgers and token fingerprints, and no personal tasks created.

Production read-only verification passed 13/13 checks. Public assets match the
release SHA256; the account table has eight columns; unauthenticated personal API
access returns 401; internal identity/source endpoints return 404 externally.
Main API, X sidecar, X Auto and the new empty personal pool scheduler are running.
All eight original trigger active/enabled states match the pre-maintenance snapshot.
Existing FB and YouTube worker/service checks are healthy.

All 67 ordinary users retain their original roles, status and permission maps.
All six administrators have the new module grant and retain existing grants.
No account ownership, publish eligibility, tokens or historical tasks were edited.

Authenticated browser checks verified 42 accounts and the eight retained columns,
the five-step manual workflow, exact material lookup `7424637`, empty personal
records, read-only settings, the paused empty personal pool and the Excel entry.
The observed 1001px viewport has no document-level horizontal overflow. Earlier
isolated UI checks cover 1440/820/390px. Console observations contain the pre-existing
browser-extension asynchronous message-channel warning; no application failure
was observed. No production batch preview/submit, configuration save, pool enable,
file upload or X publishing request was used for verification. A final read-only
check after all browser actions confirms all ten `x_video_publish_*` tables empty.

Evidence files in the backup directory: `manifest.json`, `production-readonly.json`
and `production-readonly-state.json`. Safe copies and a browser screenshot are in
`D:\codex\artifacts\x-personal-deploy-20261022`.

Exact rollback command, while no personal task has been created:

```sh
cd /mnt/data-disk/x-personal-publishing/source
python3 scripts/deploy_x_personal_publish.py --rollback /mnt/data-disk/x-post-automation/backups/20261022T120433Z-personal-30f1d0518a
```

Do not restore database or token backup files over live state. If personal tasks
exist, disable new creation first and retain the new executor until existing and
unknown-outcome tasks are reconciled; the rollback script enforces this boundary.
