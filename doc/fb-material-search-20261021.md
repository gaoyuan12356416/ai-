# Facebook material ID search repair — 2026-10-21

## Cause and scope

The reported picker request was `q=3174214&language=ja&limit=50`.
Production access logs at 16:49–16:50 Beijing time show repeated HTTP 503.
An isolated replay through the existing FIFO read-only adapter took 9.784 s;
MySQL returned ERROR 3024 (the existing 8 s statement timeout). The query
forced a PRIMARY index scan and used substring matching over ID/name plus a
correlated drama-title lookup. Because the requested row was ineligible,
LIMIT could not stop the scan after finding that ID. Concurrent UI requests
then exhausted the two adapter slots, producing `source_busy`.

A primary-key lookup with the same eligibility guards completed in 2.015 s
and correctly returned no eligible rows. A separate one-row read verified
3174214 is an active DramaWave/source-6 Japanese video but `video_duration=0`.
The patch does not invent its duration or make it publishable.

Pure ASCII digits now mean a complete material ID, clearly stated in the UI.
They use `s.id = integer` and never fall back to a substring scan, including
when missing, deleted, wrong-language or ineligible. One bounded, same-catalog
read explains an empty exact result. All publication eligibility, current
metadata, blacklist, language, uploader and URL guards remain in force.
Name/drama text searches retain their existing substring contract and SQL
deadline; this patch does not claim to solve arbitrary broad text scans.

The picker has one active request, explicit loading/error states, disabled
duplicate search/filter/page controls, persistent selections across failures,
and a closed-dialog response guard. No pool/queue/publisher mutation is made
by verification. The three HTML entry points use a new script cache version.

## Validation

- `python -m unittest tests.test_fb_video_source_links -q`: 37 passed.
- `python scripts/test_fb_video_publish_broker.py -q`: 18 passed.
- `node --check static/fb-video-publish.js`: passed.
- `node scripts/test_fb_material_search_ui.js`: delayed-response, duplicate
  request, selection retention, failure/retry, stale-dialog and language-lock
  control-flow checks passed; these use a DOM fixture, not a real browser.
- `git diff --check`: passed.

Production verification and real browser results are appended after rollout.

## Deployment and rollback

Run `scripts/deploy_fb_material_search.py` from the exact pushed GitHub commit
on CPU 43.166.187.96, first without `--apply`, then with `--apply`.
It reuses the existing hash-guarded Page-list delivery implementation in an
isolated module instance. Baseline: `3e2aee6`. Allowed runtime changes are
`features/fb_video_publish/sources.py` and the four picker static assets.

Deployment verifies the data-disk mount, live source SHA, GitHub ref and pool
schedule window; saves current service/timer states; drains FB execution;
backs up the current files and operational SQLite; replaces only the allowed
files in FB/main/Nginx locations; restarts the shared FB sidecar and restores
previous worker/timer states. Main API, app.py, SQL gate, database schema,
GPU services, publisher facts and short links are unchanged.

Rollback uses the same exact release script:

```sh
python3 /mnt/data-disk/fb-video-publish/releases/COMMIT/scripts/deploy_fb_material_search.py --rollback /mnt/data-disk/fb-video-publish/backups/BACKUP/manifest.json
```

The manifest records the exact pre-change file hashes and backup paths. The
rollback drains the same services, refuses unrelated source drift, restores
only these files and prior service states, and never restores old SQLite over
new publication facts. A later patch must be reconciled before this rollback.
