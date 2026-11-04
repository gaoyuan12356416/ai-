# FB 2446289 daily replacement/deletion job

This directory tracks the previously standalone production daily script and its
unchanged cron wrapper. The shared executor, Feishu configuration, credentials,
success ledger, and Page cache remain on the server; they are not bundled here.

## 2026-11-04 connection failure

At 10:10:20 Asia/Shanghai the 10:00 job stopped during candidate enrichment with
MySQL ERROR 2013 (`reading authorization packet`, system error 11). The SQL gate
had granted a permit. The job had screened 306,020 candidate rows, versus 704 on
the previous day, but had not begun any Meta mutation; its result JSONL was empty.
Both existing database routes responded again during diagnosis. The underlying
cause of the transient handshake interruption was not independently established.

Each single SELECT now attempts at most three times, with one- and three-second
backoffs, for connection errors 1040/1203/2002/2003/2006/2013/2055. Other errors,
timeouts, and SQL gate lifecycle failures stop immediately. Failed attempts never
contribute partial rows. The client still uses the global FIFO gate; nested gate
retries are disabled. Passwords use MYSQL_PWD, and timeout errors omit command/SQL.
The imported executor uses the same SELECT helper within this daily process.

Candidate batches remain sequential, with 800 IDs per batch and periodic progress
logs. Selection conditions, success deduplication, and Graph mutation behavior are
unchanged. There is no retry of the complete job or of ad writes.

The existing SEC log route is 63348. Metadata/token reads remain on the read-only
63350 endpoint. The SEC log table is not readable by this account on 63350
(verified ERROR 1142); this patch does not change routes or grants.

## Validation

```bash
python3 -m unittest discover -s server-monitors/fb-2446289-daily -p 'test_*.py' -v
python3 -m py_compile server-monitors/fb-2446289-daily/*.py
```

After installation, the normal self-check makes read-only DB/Graph checks and
obtains a Feishu tenant token without sending a message:

```bash
/root/codex_test/run_fb_2446289_daily_replace_delete.sh --self-check --no-notify
```

For an integration check, verify the data disk is mounted with UUID
`3e8ac4e8-7770-456d-9e89-2ec5dd405fa8`, then run the tracked verification script
with a unique directory under `/mnt/data-disk`. It reads the complete 24-hour
candidate list, takes the first five rows for enrichment, and runs the dry path.
It explicitly rejects Graph writes/uploads, execution, success-history updates,
and notifications, and copies the success ledger and Page cache into the verification directory.
It shares the production nonconcurrent lock and compares production ledger/cache
hashes. This is a sampled integration check, not a full 24-hour dry run or rerun.

```bash
python3 server-monitors/fb-2446289-daily/verify_readonly.py \
  --output-dir /mnt/data-disk/fb2446289-db-retry-20261104/verification
```

## Deployment and rollback

Push the reviewed commit first. Fetch that exact commit into a dedicated release
checkout on the mounted data disk. Save the previous script, its checksum, and
crontab in the task backup directory. Install only
`fb_2446289_daily_replace_delete.py` into `/root/codex_test`, preserving its mode,
then verify its checksum against the release checkout and run the checks above.

The cron entry remains `0 10 * * *`; this script-only change requires no service
restart. Do not manually run the live job as part of validation.

Rollback consists of copying the backed-up daily script over
`/root/codex_test/fb_2446289_daily_replace_delete.py` and syntax-checking it. Do not
restore or delete runtime state, alter the cron entry, or overwrite the shared
executor. The deployment record contains the exact commit and backup path.
