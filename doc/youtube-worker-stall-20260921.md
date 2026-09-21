# YouTube task stalls, 2026-09-21

The worker previously waited for a complete cover-generation call before checking
approved uploads again. Repeated 20-minute generations also made a due scheduled
video consume each scarce publishing turn. Preparation now runs in one bounded
background thread; the existing publisher and its durable claims remain serial.

The Codex Node launcher could time out while its native child retained captured
pipes. Generation now owns a process group and kills the group on timeout. A
complete image left behind can pass the existing frozen-reference, PNG decode,
size and crop validation and enter human review. A reported safety refusal has a
distinct error and is not retried as missing output. No review is auto-approved.

Validation: targeted runtime, reference, image and workflow regression tests;
Linux integration verifies timeout also closes a descendant's inherited pipes.
No test calls upload videos or send notifications.

Deploy only the three allowlisted files using
`python3 scripts/deploy_youtube_worker_stall.py` from a clean GitHub-fetched checkout.
The installer checks production hashes, data mount and idle publishing leases,
backs up code and the SQLite database, and restarts only the auto worker.
Rollback: `python3 scripts/deploy_youtube_worker_stall.py --rollback BACKUP_PATH`.
This restores code only. Never restore the diagnostic SQLite backup over current
video IDs, comments, schedules or asset facts.

Incident recovery: original preparation IDs 36265ae11808aa120ede8d2565878a5b
and 308f164cb373a9a0d157f1a6a5fa63ca must retain their review gates. The first has
recorded image-tool safety refusals; stop repeated attempts and expose the reason.
The second has a decodable 1672x941 output with SHA256
65c18b57ba90a949a48beec3d06d630b532bfe8c450225f5830b26726da3c985;
validate its frozen reference, normalize using the existing crop rule and recover
to review while retaining its 2026-09-22 02:57 UTC appointment.
Publishing ledgers 167 and 168 must continue through the normal engine. Ledger
167 already has video srU10gECYSQ; never create a replacement upload.


## Production verification

Deployed commit `7db11ae0f112fa033c4e8e96a8a9ad1bfd19b200` to CPU
`43.166.187.96:/root/drama_material_service`, replacing only the three allowlisted
files. Worker PID is 317191; API and unified-writer services stayed active.
Linux: 104 tests, 103 passed and one historical fixture skipped. Windows: 104
cases, 102 passed with the Linux process-group check and historical fixture skipped.
Production runtime SHA256 equals the GitHub-fetched release.

Backup: `/mnt/data-disk/deploy/youtube-auto-publish/backups/worker-stall-20260921-151652-7db11ae0f112`.
Exact rollback command:

```sh
python3 /mnt/data-disk/deploy/youtube-auto-publish/releases/worker-stall-20260921/scripts/deploy_youtube_worker_stall.py --rollback /mnt/data-disk/deploy/youtube-auto-publish/backups/worker-stall-20260921-151652-7db11ae0f112
```

The old generator did not exit on SIGTERM and systemd ended its worker cgroup
at the existing 60-second stop deadline. No active video upload lease existed
before the stop. The new process-group integration test covers the orphaned-child
failure mode. No other service was restarted.

Original ledger 167 was confirmed by authenticated API to be public, processed,
channel-matched, with its approved thumbnail already successful and no schedule.
The user-authorized incident recovery recorded that evidence, changed only the
same-video readback phase, then invoked the existing retry method. The normal
engine confirmed publication and posted its original first comment. Ledger 168
completed through the normal worker. Both have exactly one upload attempt and one
comment attempt. An early-public state is not generally safe to retry; retain the
unknown fence for unverified videos and scheduled videos.

Recovery backups and evidence:
`/mnt/data-disk/youtube-auto-publish/recovery-20260921-1789975146` and
`/mnt/data-disk/youtube-auto-publish/public-reconcile-167-1789975243`.

A separate pre-existing unified-record backlog was discovered: the legacy
publisher has been inactive since the September 15 period and held the outbox
consumer. It was not re-enabled, and historical records were not replayed.
Only the six original outbox entries for ledgers 167 and 168 were selected for
controlled idempotent RPC synchronization.

Final readback: all six selected outbox rows are synced with attempt_count=1;
both publishing ledgers are published/synced. The SSH client timed out waiting
for long command output, so completion was verified separately from SQLite and
the production file hashes, rather than re-running any publication or sync.
