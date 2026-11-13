# Private storage archive

CPU operations use `/opt/private-storage-archive/current/scripts/private_storage_archive.py`
with `/usr/bin/python3.9`. The root-only configuration is
`/etc/codex-private-storage-archive.json` (mode 0600). Never copy it into source
control, reports, service logs, or an unprivileged application environment.

The CPU host is in `na-ashburn`. Its archive bucket is
`codex-private-archive-us-1306474899`, prefix `storage-archive/v1`, with owner-only
access. Do not substitute a public media bucket. The configuration contains a
`cos` object with `region`, `bucket`, `prefix`, `secret_id`, and `secret_key`.

## Write and verify

```bash
/usr/bin/python3.9 /opt/private-storage-archive/current/scripts/private_storage_archive.py \
  put --path /absolute/immutable/file.sqlite3 --namespace project-name --json
```

`put` leaves the original in place. It hashes the original, streams an upload
(gzip for database/JSON/text files), hashes the stream again, and checks that
the source did not change. A complete remote read verifies the original SHA256
and byte length. Object ACL and an anonymous request establish private access.
The return value identifies the bucket, region, object key, hash, size,
encoding, source metadata, and durable private COS receipt. Content-addressed
objects deduplicate equal originals. This is an archive of the original bytes,
not a substitute for application-level database migration validation.

## Restore without overwriting live data

Find a `verified` or `deleted` receipt for the original path in the appropriate
JSONL journal. Use its exact object key, uncompressed size and SHA256:

```bash
/usr/bin/python3.9 /opt/private-storage-archive/current/scripts/private_storage_archive.py \
  get --key storage-archive/v1/objects/xx/HASH.gz \
  --output /absolute/recovery/file.sqlite3 --sha256 HASH --size ORIGINAL_BYTES --json
```

The command writes to a temporary file beside the target, verifies the full
content, and publishes without replacing an existing path. A different target
file or symbolic link is rejected, including one created during download.
The restored file has mode 0600. Restore the owner/group/mode from the receipt
only after selecting the correct recovery path and stopping affected writers.
For SQLite, run `PRAGMA quick_check` before switching a service back to it.
Never restore over an actively used database or mix an old main file with a
newer WAL. The FB metrics cutover has a separate application rollback helper.

The first 2026-11-13 receipts reference the older private Hong Kong bucket.
Those objects were also copied and fully verified in the US bucket. Their
original region remains available in the saved root-only Hong Kong config;
select it with the global `--config` argument if restoring that original copy.

## Verified cleanup

`batch --plan PLAN --manifest JOURNAL --delete-verified` accepts only explicit
paths beneath the plan's allowed roots and their previously captured file
signatures. It writes and fsyncs the verification receipt before checking the
source again and unlinking. Changed/open files are preserved. The deletion
receipt records actual allocated blocks released, accounting for hard links.
Do not generate cleanup plans from directory age alone. Freeze writers first;
the open-file check is not a lock against concurrent writers.

The 2026-11-13 plans, journals, baseline, and restore proof live under
`/mnt/data-disk/storage-migration-20261113`. Journals preserve original paths
and object identities. Keep them for recovery and reconcile every planned
missing file against a successful deletion receipt. Report verified released
blocks separately from the disk's net free-space increase while other jobs run.

Runtime application data, active queues, publishing receipts, unfinished
device-insights runs, and media still served by nginx are outside cold-backup
cleanup. Weekly reports and catalog exports require their dedicated verified
ads_ai migrations and consumer cutovers before removing original large files.
