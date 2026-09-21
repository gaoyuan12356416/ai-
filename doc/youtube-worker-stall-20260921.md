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
