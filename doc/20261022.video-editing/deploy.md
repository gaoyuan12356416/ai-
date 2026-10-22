# Deployment and recovery

## Scope

The CPU runtime is a composite installation with unrelated changes. Never copy a whole checkout over it. `scripts/deploy_video_editing_cpu.py` checks pinned live app/navigation hashes, applies AST-scoped dispatch/permission patches, installs only the new package/static files/unit, and keeps intake disabled for acceptance. Its source must be a clean exact GitHub commit. CPU metadata and backups live on the verified data disk under `/mnt/data-disk/video-editing`.

The GPU branch is based on the actually running `223577b41466d03fd330625441ab3b7c1d86e843`. Only the mixed runtime, worker, new editor package and validation/deployment files are added. Do not deploy the CPU branch's app or older publishing modules to the GPU. Candidate releases must use `/data/drama-synthesis-gpu/releases/<40-character-commit>` so the existing runtime preflight remains authoritative.

## Release procedure

1. Push the reviewed CPU and GPU commits to GitHub. Fetch exact commits on each host and verify clean source status.
2. Back up current unit/config/link/file hashes and take online SQLite evidence backups. Preserve all prior GPU records and result receipts.
3. Pause new CPU media claims briefly and wait for existing affected jobs, GPU runtime records and child processes to drain. Do not stop or modify social publication services.
4. Run candidate GPU preflight and isolated synthetic render/COS acceptance. Candidate tests must run only while the single render slot is idle; no second persistent renderer is introduced.
5. Install the GPU mixed reader with editor intake disabled. Restart only the GPU worker and its reverse tunnel; verify release, profile, queue limit and concurrency.
6. Run CPU installer dry-run with the verified app/navigation SHA256, inspect its exact file manifest, then use `--apply`. It rechecks idle after stopping only the main API, validates health, and verifies protected service identities.
7. Configure private objects and the scoped COS CORS rule for `https://ai.yingliangads.com`. Preserve all prior CORS rules and save the previous policy. The bucket's existing public objects remain governed by their original policies; every editor source, thumbnail, proxy and output has explicit private object ACL.
8. Start `video-editing-worker.service`, remove the editor intake marker on GPU then CPU, and validate authenticated routes, direct multipart upload and a two-drama batch. Do not grant ordinary users this new permission automatically; administrators assign `video_editing` through the existing user management page.

## Settings and evidence

See `deploy/video-editing.env.example`. The CPU worker inherits existing private `.env` and `/etc/drama-synthesis/cpu.env`; no keys are copied into Git, responses or browser storage. The new module uses prefix `video-editing/v1`, separate metadata SQLite and GPU temporary/result subdirectories. Durable evidence is stored under `/mnt/data-disk/video-editing/validation` and each install backup contains its exact commit and per-file hashes.

`scripts/validate_video_editing_cos.py` is an isolated metadata-only acceptance helper; it does not accept video bodies. Its signed responses must be consumed in memory by the local uploader and never logged. Synthetic media originates on the workstation and travels directly to COS. Existing business task databases are read only during these canaries.

## Rollback

After any editor task is accepted, first pause new intake on BOTH hosts:

```sh
install -d -m 700 /etc/video-editing
touch /etc/video-editing/intake.disabled
chmod 600 /etc/video-editing/intake.disabled
```

Keep the mixed GPU runtime, all SQLite rows, frozen recipes, upload checkpoints, result receipts and COS objects. Existing GET/reconcile and explicit same-task recovery remain available. Disable the new navigation group using its saved configuration if necessary. Restore a previous page or the new CPU route code only after queued requests are accounted for; do not restore a historical database or roll the GPU binary back to a reader that cannot understand editor tasks. The first-install scripts can restore their pre-install files automatically before intake has ever opened; this is not a general post-acceptance rollback strategy.

Successful scratch is removed only after authenticated COS receipt verification. Stopped failed tasks retain scratch for 24 hours and are then expired for in-place recovery; records and completed objects remain durable. A changed recipe is a new project version and task, never an overwrite of an earlier successful output.

## Acceptance status

Implementation and local tests are complete. Live release SHAs, installation backup paths, actual COS/GPU receipts and any remaining acceptance limitations are recorded in the final test report after validation; a prepared release is not a deployed release.
