# YouTube cover provenance

The worker previously accepted any decodable, correctly sized `cover.png` after
the generator exited. The generator inherited a shared Codex home containing
historical images. Production evidence showed byte-identical outputs reused
across unrelated dramas, including an image dating from May in a September task.
The reference cover and drama metadata were correct in the reported task.

Each attempt now gets a fresh private Codex home with only CLI authentication
and model discovery metadata. Global config/rules are ignored; the model and
reasoning are explicit environment settings, defaulting to the verified current
production values. Application credentials remain excluded from the environment.
The temporary authentication copy is deleted after each attempt.

Acceptance requires the CLI JSON stream's unique thread ID, a regular native
image under that invocation's `generated_images/<thread-id>` directory, fresh
modification time, and exact SHA-256 equality with the submitted output. A hash
already accepted by any earlier generation attempt is rejected. Final model text
is not evidence of generation. The audit records thread, native artifact, source
hash and frozen reference hash. Timeout recovery requires the same evidence.

This prevents historical-file substitution. It does not claim to prove semantic
character/title fidelity for a newly generated picture; human cover review remains
required. Manual covers, previously approved assets, schedules, upload ledgers,
platform videos and comments are unchanged by code deployment.

## Validation

Run `python -m unittest scripts.test_youtube_cover_provenance
scripts.test_youtube_reference_runtime scripts.test_youtube_failure_images_runtime
scripts.test_youtube_cover_crop scripts.test_youtube_worker_runtime -q`.
The tests cover fresh artifacts, unrelated output bytes, stale/other-thread files,
duplicate hashes, symlinks, unsupported completion claims, reference integrity,
image decoding/cropping and subprocess cleanup. Also run the existing service and
reference-workflow suites, plus a real isolated image generation before release.

Deploy only the three Python modules using
`python3 scripts/deploy_youtube_cover_provenance.py` from a clean GitHub-fetched
release. It checks exact prior file hashes, an idle worker, data disk identity,
and backs up code plus a consistent database snapshot. Only the YouTube automatic
publishing worker restarts. Roll back with the same release script and
`--rollback <backup-path>`; current databases, images and publication facts remain.
