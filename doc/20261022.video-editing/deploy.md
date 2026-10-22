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

## Actual release, 2026-10-22

- CPU `43.166.187.96:/root/drama_material_service`: new package/page/worker from `0d4af1beee51572ab61b37e0cdfca52546923a70`, followed by the exact four-line local `sys` import repair and new Nginx location from `c6d394ab39bdabacfefde1d33890b583eb03adb6`. The new module payload is identical at these commits. No whole checkout replaced the composite app. Final app SHA256 is `6db68caaf87679bf52b84e6530b6ee92913f751e31ac23d3805e2eae9b8bf3e0`.
- CPU backups: `/mnt/data-disk/video-editing/backups/20261022T131206-0d4af1beee51` (26-file first install, SQLite evidence), then `/mnt/data-disk/video-editing/backups/integration-20261022T131652-c6d394ab39bd` (app integration repair). `/etc/nginx/default.d/video-editing.conf` is scoped to `/api/video-editing/`, accepts at most 2 MiB metadata JSON and buffers that body in memory. `nginx -t`, graceful reload, API restart and public unauthenticated JSON 401 readback passed.
- GPU `43.154.250.89:/data/drama-synthesis-gpu/current`: exact release `81f75eca25bd472cddec21ccbd2e09b5f728fe51`, based on the actual previous source overlay `223577b41466d03fd330625441ab3b7c1d86e843`. Backup: `/data/drama-synthesis-gpu/deploy-backups/video-editing-20261022T050848Z`.
- GPU candidate and systemd preflight passed (80 assets, compositor, CUDA, guarded import). The install helper initially checked HTTP before the new listener became ready and entered its guarded error path. It retained the new reader. Independent readback then verified exact release, legacy profile, environment, one render slot, idle media requests and all 23 old ledger hashes unchanged; `result-reconciled.json` records that outcome. This is an explicitly reconciled installation, not a claimed clean helper exit.
- The bounded listener-start fix is pushed in CPU `c4cf6a92d89a9a5bb1e26e6105ca14346f5d0d3c` and GPU `8c5d1b95de07c0108e056b420bdbc94db72ba7cc`. It changes only the deployment helper/tests; the running GPU media code remains the verified `81f75eca` release. No unnecessary second production restart was performed.
- `video-editing-worker.service` is enabled and active. Both intake markers were removed after health/recovery verification. Ordinary user permission defaults remain false; admins use the existing assignment flow. Existing publishing service identities were preserved.

## Acceptance evidence and limits

Private operational receipts remain outside Git under the workstation's `output/video-editing-validation`, CPU `/mnt/data-disk/video-editing/validation`, and GPU `/data/drama-synthesis-gpu/validation/recovery/completed_restart_20261022_a`. They contain object identities and checksums, not persistent credentials or printed temporary signatures.

Two synthetic timelines passed real GPU rendering, complete decode, per-frame cut/fade/geometry comparison, audio interval checks, COS receipt validation, cached repeat execution and successful scratch cleanup. An isolated instance of the exact released HTTP/AsyncRuntime completed an editor task, stopped its positively identified listener, restarted with the same state, and returned the unchanged result and generation on GET and repeat POST. COS ETag, LastModified and SHA remained unchanged. The live worker and old ledgers were not modified by this isolated test.

The authenticated Chrome page, real drama catalog, episode probes and editing controls were exercised. Chrome's automation extension requires the user's file-URL permission before it can choose a local upload file; that request is pending. A clearly named synthetic acceptance asset was instead uploaded directly from the workstation through the standard production service's create/sign/complete workflow under an explicit system-acceptance actor. This permits continued UI rendering tests and does not count as browser-file-selection acceptance. No account, login session or permission was forged or changed.

Mid-download/process-crash fault injection, an actual mixed production workload and a full 8 GiB browser upload were not performed. Existing deterministic recovery/queue tests and the real completed-task restart are separate evidence, not substitutes for those unexecuted cases. See `test-report.md` for final real two-drama result readback.

## Exact post-acceptance rollback procedure

On **both** CPU and GPU, run:

```sh
install -d -m 700 /etc/video-editing
touch /etc/video-editing/intake.disabled
chmod 600 /etc/video-editing/intake.disabled
```

This closes new uploads/probes/render submissions while preserving reads, existing upload completion and same-task recovery. Keep the CPU coordinator running to reconcile accepted tasks, keep GPU release `81f75eca` or a newer compatible mixed reader, and retain all ledgers/COS outputs. The administrative navigation setting can then hide the new entry. Do not restore the pre-install SQLite evidence copy or the old GPU reader after editor acceptance. Source context for this feature is maintained in this repository's requirement/interface/test/deploy documents; personal skill and memory files were not modified.
