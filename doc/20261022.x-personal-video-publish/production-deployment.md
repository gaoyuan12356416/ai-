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

The installer pauses only previously active X triggers, waits for workers and
YouTube share requests to finish, and acquires shared publisher locks. It backs
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
