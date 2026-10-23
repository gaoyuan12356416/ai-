# Exact material IDs: one gated database request

The previous source-ID lookup opened three mysql clients sequentially: source
record, drama resources and deployment information. ID 7424637 used PRIMARY,
content_id and ac indexes, but the complete read took 6.8144 seconds while the
individual SQL statements took approximately 1.6, 4.1 and 2.0 milliseconds.

Exact ID searches and explicit selected-ID revalidation now use one SELECT with
three UNION ALL branches through the existing FIFO-gated read-only adapter.
Source IDs remain bound parameters. The mapping branches start from at most 200
distinct source content IDs and retain their existing indexes, app 1479 scope,
independent 4001-row overflow detection, complete mapping rows and validation.
Keeping the branches separate avoids multiplying resource/deployment rows.
There is one client, one gate admission and one database connection per lookup.

Browse and text/title search keep their existing query paths. No source cache,
schema/index mutation, SQL timeout increase, gate bypass or publishing operation
is added. Explicit-ID reads retain cross-product behavior, language/cursor
filtering and bounded missing-duration recovery. Preflight continues to reread
current data through the same combined path.

Local validation: 113 personal-publishing backend tests passed, including
one-request behavior, binding, pagination/filtering, conflict and overflow
rejection, metadata recovery, null/Unicode round trips and malformed result
rejection. Git diff whitespace check passed.

Deploy only features/x_video_publish/sources.py with the guarded installer
scripts/deploy_x_material_single_query.py, based on production 37de345.
The installer snapshots affected code/SQLite/tokens, drains workers, checks live
file hashes, restores original service and trigger states, and verifies ledger
and token fingerprints. Its rollback restores only the preceding code file.

## Production acceptance

- Release: 4317b66d13f0e39dce3d46faf9146e89586a248a, pushed to GitHub and
  checked out at /mnt/data-disk/x-personal-publishing/material-single-query-4317b66.
- CPU: 43.166.187.96. Updated the one repository module in
  /root/drama_material_service and /opt/x-post-automation/current (two files).
- Backup: /mnt/data-disk/x-post-automation/backups/20261023T073938Z-source-4317b66d/manifest.json.
- Deployment status: deployed; both installed hashes match. Publisher ledger and
  token fingerprints were preserved, all original trigger states restored, and
  drama-material-api, x-post-automation, x-auto-post-service and
  x-video-pool-scheduler are active.
- Windows and Linux each passed:
  `python -m unittest discover -s scripts -p 'test_x_video_publish_*.py'`
  (113 tests); `git diff --check` passed locally.
- Candidate and previous live source responses matched exactly for 7424637,
  7781416 and their two-ID revalidation. Live Japanese/ja/jp, incorrect English,
  cursor exclusion and nonexistent-ID checks behaved as expected. EXPLAIN still
  shows PRIMARY for exact source reads, content_id for resources and ac for
  deployment information; only bounded derived results are scanned.

| ID / condition | Before (seconds) | After deployment (seconds) |
| --- | ---: | --- |
| 7424637, existing duration | 6.5733 | 2.6671, 2.5406, 2.4642 (mean 2.5573) |
| 7781416, first duration read | 10.1436 | 5.6543 |
| 7781416, measured duration cached | not measured in this comparison | 2.4641, 2.6397 |

Before measurements ran the previous source repository against the live replica;
after measurements called the restarted production source HTTP service, including
the gate, database connection, mapping validation and applicable metadata read.
These are server-side samples, excluding public-browser network transit. No
source-row result cache was added. All result digests matched the previous code.

The public browser also returned ID 7424637 with its Japanese title and 861-second
duration; Nginx recorded HTTP 200. A timed repeat from clicking Search to the
checkbox becoming visible took 2.928 seconds, including automation control
latency. No materials were selected and no verification Post was created.
The local evidence directory D:/codex/artifacts/x-material-single-query-20261023/
contains acceptance.json, deployment.json, browser-timing.json and
material-7424637.png.

Exact rollback on the CPU server (code only; current publisher data is retained):

```sh
cd /mnt/data-disk/x-personal-publishing/material-single-query-4317b66
python3 scripts/deploy_x_material_single_query.py --rollback /mnt/data-disk/x-post-automation/backups/20261023T073938Z-source-4317b66d/manifest.json
```
