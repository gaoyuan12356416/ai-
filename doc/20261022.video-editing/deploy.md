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
install -d -m 755 /etc/video-editing
touch /etc/video-editing/intake.disabled
chmod 644 /etc/video-editing/intake.disabled
```

Keep the mixed GPU runtime, all SQLite rows, frozen recipes, upload checkpoints, result receipts and COS objects. Existing GET/reconcile and explicit same-task recovery remain available. Disable the new navigation group using its saved configuration if necessary. Restore a previous page or the new CPU route code only after queued requests are accounted for; do not restore a historical database or roll the GPU binary back to a reader that cannot understand editor tasks. The first-install scripts can restore their pre-install files automatically before intake has ever opened; this is not a general post-acceptance rollback strategy.

Successful scratch is removed only after authenticated COS receipt verification. Stopped failed tasks retain scratch for 24 hours. After cleanup, only a fingerprint-bound expiry with no upload intent or unknown evidence and authenticated 404 responses for every fixed output key may resume under the original task ID, using a new generation and isolated local checkpoint directory. Unknown uploads remain fenced; records and completed objects remain durable. A changed recipe is a new project version and task, never an overwrite of an earlier successful output.

## Actual release, 2026-10-22

Current state after final readback: CPU feature integration remains the scoped `c6d394ab` payload plus the `9fc968a` metadata fix; GPU is now the compatible mixed-reader release `1747c5cd560903032b90ce18d961bc1fe3caddb6`. Both intake gates are open, the API and two CPU coordinators are active, the GPU worker and reverse tunnel are active, and the two formal completed outputs retain generation 1 and their original completion times. The older entries below are the rollout history, not alternative current releases.

- CPU `43.166.187.96:/root/drama_material_service`: new package/page/worker from `0d4af1beee51572ab61b37e0cdfca52546923a70`, followed by the exact four-line local `sys` import repair and new Nginx location from `c6d394ab39bdabacfefde1d33890b583eb03adb6`. The new module payload is identical at these commits. No whole checkout replaced the composite app. Final app SHA256 is `6db68caaf87679bf52b84e6530b6ee92913f751e31ac23d3805e2eae9b8bf3e0`.
- CPU backups: `/mnt/data-disk/video-editing/backups/20261022T131206-0d4af1beee51` (26-file first install, SQLite evidence), then `/mnt/data-disk/video-editing/backups/integration-20261022T131652-c6d394ab39bd` (app integration repair). `/etc/nginx/default.d/video-editing.conf` is scoped to `/api/video-editing/`, accepts at most 2 MiB metadata JSON and buffers that body in memory. `nginx -t`, graceful reload, API restart and public unauthenticated JSON 401 readback passed.
- GPU `43.154.250.89:/data/drama-synthesis-gpu/current`: exact release `81f75eca25bd472cddec21ccbd2e09b5f728fe51`, based on the actual previous source overlay `223577b41466d03fd330625441ab3b7c1d86e843`. Backup: `/data/drama-synthesis-gpu/deploy-backups/video-editing-20261022T050848Z`.
- GPU candidate and systemd preflight passed (80 assets, compositor, CUDA, guarded import). The install helper initially checked HTTP before the new listener became ready and entered its guarded error path. It retained the new reader. Independent readback then verified exact release, legacy profile, environment, one render slot, idle media requests and all 23 old ledger hashes unchanged; `result-reconciled.json` records that outcome. This is an explicitly reconciled installation, not a claimed clean helper exit.
- The bounded listener-start fix was pushed in CPU `c4cf6a92d89a9a5bb1e26e6105ca14346f5d0d3c` and GPU `8c5d1b95de07c0108e056b420bdbc94db72ba7cc`. It changes only the deployment helper/tests, so at that stage the GPU stayed on `81f75eca` without an extra restart. The subsequent BUG-007 update below includes this helper fix.
- `video-editing-worker.service` is enabled and active. Both intake markers were removed after health/recovery verification. Ordinary user permission defaults remain false; admins use the existing assignment flow. Existing publishing service identities were preserved.
- CPU metadata follow-up `9fc968a98924e658895240a8763162a2b97c207f` was fetched from GitHub and deployed only to `features/video_editing/service.py` and `store.py`. Server service tests passed 29/29. Backup: `/mnt/data-disk/video-editing/backups/idempotence-20261022T055151Z-9fc968a98924`. The additive render-operation table was created, only `drama-material-api.service` restarted (new PID 3770172), and the editor coordinator, old job worker and protected publishers retained their identities. New file SHA256: service `740f600d3c2aaf71927c6ffa1856350f51034c34eb4a276b8cf4e9b26e0601dd`, store `51a915f065e3c5498c640ed236faa823d3fcb60810632927ffdd8f33f7018821`.
- BUG-007 core was pushed in CPU source `f29f45ff51946a2e5709137d91e7539f6e36041f` and GPU source `1747c5cd560903032b90ce18d961bc1fe3caddb6`. After isolated acceptance, the GPU fetched that exact clean commit from GitHub. Candidate and systemd startup preflights passed. The compatible update changed only the current release symlink and `DRAMA_GPU_RELEASE_SHA`, using the existing mixed scheduler and unchanged queue/concurrency profile. It did not invoke the first-install rollback helper. Backup: `/data/drama-synthesis-gpu/deploy-backups/compatible-update-20261022T061934Z-1747c5cd5609`, including configuration, all ledger hashes and `result.json` / `final-readback.json`.
- Final independent readback: GPU PID 1560374, reverse tunnel active, render concurrency 1, queue capacity 8 from the unchanged code default, 29 existing records with identical hashes, zero active jobs. The API stayed PID 3770172; the briefly paused old claimant and editor coordinator restarted as PIDs 3797297 / 3797300. Protected publishing PIDs 2937249 / 2576490 never changed. Both intake markers are absent. Evidence: CPU `/mnt/data-disk/video-editing/validation/final-production-readback.json`, GPU backup `final-readback.json`, and local `output/video-editing-validation/final-production-readback.json`.
- After that readback, the authenticated Chrome task page was refreshed and still displayed both original successful outputs, 12.0 seconds and 9.7 seconds. The final screenshot is `output/video-editing-validation/production-completed-tasks.png`.

## Acceptance evidence and limits

Private operational receipts remain outside Git under the workstation's `output/video-editing-validation`, CPU `/mnt/data-disk/video-editing/validation`, and GPU `/data/drama-synthesis-gpu/validation/recovery/completed_restart_20261022_a`. They contain object identities and checksums, not persistent credentials or printed temporary signatures.

Two synthetic timelines passed real GPU rendering, complete decode, per-frame cut/fade/geometry comparison, audio interval checks, COS receipt validation, cached repeat execution and successful scratch cleanup. An isolated instance of the exact released HTTP/AsyncRuntime completed an editor task, stopped its positively identified listener, restarted with the same state, and returned the unchanged result and generation on GET and repeat POST. COS ETag, LastModified and SHA remained unchanged. The live worker and old ledgers were not modified by this isolated test.

The authenticated Chrome page, real drama catalog, episode probes and editing controls were exercised. The user confirmed login but could not locate Chrome's extension file-URL permission, so automated browser file selection/upload remains unverified. Normal manual use of the upload page does not require that automation permission. A clearly named synthetic acceptance asset was uploaded directly from the workstation through the standard production service's create/sign/complete workflow under an explicit system-acceptance actor. This permits continued UI rendering tests and does not count as browser-file-selection acceptance. No account, login session or permission was forged or changed.

The formal Chrome two-drama project `vp_44eb51c33d7d4e049fb02afc4f6ad1c5`, version 16, completed both tasks: `ve_1cd8849bcc86492b8ebadbcdb7a440f2` (360 frames, 12.000 seconds) and `ve_84fd6aa852844b9a930a5d17176fc8c3` (291 frames, 9.700 seconds). Both real files fully decoded, matched COS SHA/size, and have H.264/AAC 720x1280 30fps with identical video/audio durations. Generation stayed 1; task two started after task one completed, successful scratch was absent, anonymous objects returned 403 and all 23 old ledger hashes stayed unchanged. See `output/video-editing-validation/formal-output-acceptance-summary.json` and `production-finished-media-validation.json` in the private operational evidence directory.

After deploying `9fc968a`, Chrome reloaded, reopened the unchanged version 16 and submitted with a new client operation. CPU readback proved one original batch, two original completed tasks/outputs and one new operation alias; GPU readback retained both original completed timestamps and generation 1. Receipt: `/mnt/data-disk/video-editing/validation/browser-reload-idempotence.json`. This is a real browser reload/re-submit check in addition to concurrent-tab unit tests.

Mid-download/process-crash fault injection, an actual mixed production workload and a full 8 GiB browser upload were not performed. Existing deterministic recovery/queue tests and the real completed-task restart are separate evidence, not substitutes for those unexecuted cases. See `test-report.md` for final real two-drama result readback.

BUG-007's candidate `1747c5cd560903032b90ce18d961bc1fe3caddb6` passed an isolated real recovery before rollout. Task `ve_0acb6a25ed8c5137aee7286f6df8a7a4` failed at disk preflight in generation 1 with no media/download/upload calls. The test supplied `now = heartbeat + 86401` to retention cleanup; it did not wait 24 hours or change the system clock. The original task resumed by generation CAS, rendered and uploaded once in generation 2, then repeated resume/submit and a runtime restart reused the same result without additional download/render/upload calls. The private object returned anonymous 403, successful scratch was absent, all isolated processes stopped and GPU compute was idle. Live worker identity and every existing ledger hash were unchanged. Evidence: `/data/drama-synthesis-gpu/validation/bug007/preupload_expiry_20261022_a/acceptance.json`, with the local safe summary `output/video-editing-validation/bug007-preupload_expiry_20261022_a-results.json`.

## Exact post-acceptance rollback procedure

On **both** CPU and GPU, run:

```sh
install -d -m 755 /etc/video-editing
touch /etc/video-editing/intake.disabled
chmod 644 /etc/video-editing/intake.disabled
```

This closes new uploads/probes/render submissions while preserving reads, existing upload completion and same-task recovery. Keep the CPU coordinator running to reconcile accepted tasks, keep GPU release `1747c5cd` or a newer compatible mixed reader, and retain all ledgers/COS outputs. The marker directory must remain traversable by the non-root GPU worker (0755 directory, 0644 marker); secret environment files remain 0600. The administrative navigation setting can then hide the new entry. Do not restore the pre-install SQLite evidence copy or the old GPU reader after editor acceptance. Source context for this feature is maintained in this repository's requirement/interface/test/deploy documents; personal skill and memory files were not modified.

## BUG-008 页面交互更新方案（2026-10-22）

本次范围仅 `static/video-editing.html`、`.css`、`.js`，缓存版本 `20261022-responsive-actions-1`。标准布局及 shared quick-nav/ui-topbar 保持；CPU/GPU逻辑、账本、配方、任务及COS成片不变。先提交推送 GitHub，再从确切提交取三个文件，分别原子替换 `/usr/share/nginx/html` 与 `/root/drama_material_service/static`。发布前比对本轮 live manifest、数据盘 UUID、服务身份和候选清洁状态，备份六个目标文件；发布后校验公网字节哈希与既有进程 PID，不重启服务。

此次页面回滚可仅还原六个目标文件至上一标准样式版本 `866e9d6e4d4a5b7578999fcf65de38b933ac3948`，保留全部后台/GPU兼容版本和业务状态；无需关闭制作或恢复历史数据库。具体新提交、备份路径和回读结果在发布完成后补充。

## BUG-008 实际静态发布回读

2026-10-22 15:27（北京时间），GitHub已推送分支 `codex/video-editing-responsive-actions-20261022`，CPU从确切提交 `ecb040032cb2c96d5b45056be5a611c0c514b178` 拉取并完成六个静态目标文件更新。公网HTML/CSS/JS均HTTP200，字节SHA与提交一致；shared quick-nav/ui-topbar校验不变。API、编辑worker、旧制作worker及两个YouTube发布服务的前后PID分别保持3770172、3797300、3797297、2937249、2576490，未重启任何服务，也未修改GPU。

备份：`/mnt/data-disk/video-editing/backups/responsive-20261022T072730Z-ecb040032cb2`。仅回滚本次页面：

```sh
python3 /mnt/data-disk/video-editing/backups/responsive-20261022T072730Z-ecb040032cb2/rollback.py
```

该命令先验证六个目标与备份哈希及允许路径，再原子恢复上一标准样式版本。它不恢复数据库、任务或GPU版本。部署回执和公网哈希为 `output/video-editing-responsive-validation/deployment.json`；服务端备份内 `manifest.json` 保存相同证据。

本地审查JS为Windows CRLF，发布Git blob为LF；已逐字节确认仅换行差异。Git/public JS SHA256 `c4d0f0c0ce583561b079b063ac6f84244b6695ab5917ae4218e7f89bb2189f00`。本次新增一条本地技能上下文规则并保留其余条目；个人技能仓库已有未提交改动，本次未混合提交该仓库，memory文件未修改。

最终视觉复核发现取消按钮隐藏时主操作被flex靠左，追加一条CSS将“添加所选剧集”固定靠右，保持后台弹窗惯例；缓存版本递增为 `20261022-responsive-actions-2`。JS逻辑不变，51项逻辑测试和慢请求验收仍对应同一Git JS blob；仅复查按钮位置与公网静态文件。

最终上线提交为 `8d1e9c0de72037f97993ad5ecd9426d41d1e6f66`（15:31北京时间），缓存版本 `20261022-responsive-actions-2`。视觉修正再次通过同样的六文件/公网哈希/原进程验证。最新备份 `/mnt/data-disk/video-editing/backups/responsive-20261022T073147Z-8d1e9c0de720`，JS blob与首发完全一致。当前若需完整回滚BUG-008至 `866e9d6`，必须先还原对齐补丁，再还原交互补丁；以下两步均有当前文件哈希保护：

```sh
python3 /mnt/data-disk/video-editing/backups/responsive-20261022T073147Z-8d1e9c0de720/rollback.py
python3 /mnt/data-disk/video-editing/backups/responsive-20261022T072730Z-ecb040032cb2/rollback.py
```

仅执行第一步会退到已修复交互的 `ecb0400`；按顺序执行两步才回到本次修改前的标准页面。两步均保留后端、GPU及所有业务数据，不重启服务。
