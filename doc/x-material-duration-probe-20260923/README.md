# Missing X material durations

Exact pool/manual videos with a null, empty or zero `video_duration` now read
MP4 movie/track parameters with bounded HTTPS ranges, skipping the video payload.
Range responses must match exact offsets, total size and a stable strong ETag;
metadata is limited to 8 MiB and 32 top-level boxes, with a required video track.
Unsupported metadata falls back to a bounded complete download and local ffprobe.
Both paths enforce the existing HTTPS host and 512 MiB size boundary.
Existing positive durations and images avoid this extra
read. Invalid metadata remains rejected. A failed measurement remains
`material_duration_missing`; it does not prevent another valid item from selection.
Successful measurements still enforce the selected duration ceiling, mapping,
language, account and final media checks. Source MySQL rows are never modified.

Fallback temporary files use the existing verified data-disk `media-work` directory and
are deleted on success or failure. Each process limits probing to two concurrent
downloads and caches at most 256 successful URL/allowlist measurements for 15
minutes. Final publish preflight always downloads and probes again. The allowlist
uses `X_POST_SOURCE_DURATION_ALLOWED_HOSTS`, then the daily or sidecar media host
setting. Configure the dedicated setting for the main API, whose environment does
not normally include daily runner configuration. Empty configuration fails closed.

Historical unbound missing-duration errors can be scanned again. Enumeration
does not clear errors or change queue occupancy. Use the existing audited media
backfill only for explicit unbound IDs after backup; full preflight must succeed.

## Deployment

Fetch the pushed commit and overlay selector.py, service.py and source_duration.py
on the verified live composite. Align these files in main API and X runtimes.
Drain active publishers before taking their shared lock; back up code, SQLite and
non-secret token metadata. Restart only affected services and restore prior timer
states after health, hash and ledger verification. Do not publish a test Post.

Rollback restores the previous release, main API files and dedicated main API
allowlist drop-in, then restarts the affected services. Preserve current SQLite,
tokens and publication facts. Private production evidence stays outside Git.
