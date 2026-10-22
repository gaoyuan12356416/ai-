# Drama composition episode-range identity

Creating 20–40 episodes used to fail when the same product/drama already had a
1–20 task. New-task identity is now `(app_id, content_id, episode_start,
episode_end)`. Different, including overlapping, ranges are allowed. Identical
ranges retain duplicate protection across all statuses; failed tasks use the
existing retry action. Output options and resolution do not change identity.

The early read uses exact SQL rather than filtering a paginated list. The insert
also checks identity under SQLite `BEGIN IMMEDIATE` through the caller's commit,
so concurrent requests cannot both create the same range. Existing job updates,
worker lease fences, retry semantics, output URLs and historical records remain
intact. There is no schema migration or historical task retry/backfill.

Outputs and work directories are already isolated by job ID. Random-template
recipes continue to freeze per job ID before exposing the queued task.

## Validation and deployment

`python scripts/test_drama_episode_range.py` exercises the real extracted app
submit/upsert functions with disposable SQLite and a disabled worker dispatch.
Coverage includes different/overlapping ranges, all duplicate states, product
and drama isolation, updates/retries, duplicates older than 100 rows, eight
simultaneous requests, invalid ranges and SQLite Row mode. Repeat against the
patched live source with `DRAMA_RANGE_APP_SOURCE` set to its path.

Push the release, fetch it on CPU 43.166.187.96 and use a clean detached checkout.
Run `scripts/deploy_drama_episode_range.py --check` then the same script without
`--check`. It checks exact live hashes, patches only the affected functions and
page hint in the live composite, backs up code and online SQLite on the mounted
data disk, waits for an idle job state and restarts only the main API. Existing
rendering and publishing workers must keep their PIDs. The helper module is the
only newly installed runtime file. Public and main-runtime HTML copies match.

Rollback uses the exact release script with `--rollback <backup-directory>`.
The installer rejects later code drift and restores code only; retain the live
database and all range tasks and outputs created after deployment. Restoring
the old duplicate rule blocks new ranges again but does not delete existing ones.

Validation does not authorize creating a production render or publishing a Post.
