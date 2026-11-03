# Deployment and rollback

## Active release

- Host: **43.154.250.89** (Hong Kong GPU).
- GitHub: gaoyuan12356416/ai-, branch codex/pipeline-dispatch-20261102.
- Runtime: **53e7016651b8e260d9b646b1fb82c03709a53c5a**.
- Path: /opt/pipeline-dispatch/releases/53e7016651b8e260d9b646b1fb82c03709a53c5a.
- Service: pipeline-node-hk-gpu.service; **active/running since 2026-11-03 17:37:33 CST**, supervisor PID 220359 at verification.
- Flag: RANDOM_GPU_PIPELINE_V2=1; existing RGBA cache retained.
- Final backup: /data/pipeline-dispatch/maintenance/gpu-offload-20261103T092214Z (private manifest, service unit and environment; previous runtime 7c6866d).

Code was pushed to GitHub before server fetch and immutable checkout. Service-user pipeline_node_install.py --verify exercised actual decode/compositor/encoder/COS capabilities. Final SSH client disconnected, but the installer completed and left its verified marker; activation validated that marker and clean exact SHA.

Activation stops new claims and waits for active single tasks to finish. It uses the existing audited SIGTERM drain, KillMode=mixed and infinite stop timeout. It does not reset ledger, artifacts, receipts, credentials, account locks or capacity. CPU node/API services were not restarted by this GPU rollout.

At 17:44 CST central config v82 and live workers agree: GPU prepare6/publish4, CPU prepare4/publish6, global publish10, all manual. Frontend updates remain authoritative and lowering a pool waits for active jobs to finish.

## Verification

See test-report.md for 105 passing regressions, actual Chromium capacity editing, fixed-input quality/sync metrics, real six-process CUDA/OpenCL/NVENC commands and performance limits.

Normal final-release task5553 completed preparation and publication at generation1 with one final Post ID2117552183743000859. Earlier GPU task5540 was published by CPU using its frozen COS artifact. All originally captured in-flight preparations finished naturally. Five old unknown uploads remain fenced and unreplayed; they are not represented as successful publications.

## Exact rollback

Run on **43.154.250.89** to return to verified 7c6866d using the same natural drain:

```sh
/opt/pipeline-dispatch/runtime/python/bin/python3.10 /opt/pipeline-dispatch/releases/53e7016651b8e260d9b646b1fb82c03709a53c5a/scripts/activate_pipeline_gpu_offload.py --current /root/pipeline-node-gpu.json --candidate /data/pipeline-dispatch/maintenance/gpu-offload-20261103T092214Z/install-manifest.before.json --apply
```

For complete rollback of GPU source offload to cache-enabled 95fdaa4c, use:

```sh
/opt/pipeline-dispatch/runtime/python/bin/python3.10 /opt/pipeline-dispatch/releases/53e7016651b8e260d9b646b1fb82c03709a53c5a/scripts/activate_pipeline_gpu_offload.py --current /root/pipeline-node-gpu.json --candidate /data/pipeline-dispatch/maintenance/gpu-offload-20261103T084113Z/install-manifest.before.json --apply
```

If already naturally draining, add --replace-pending; the helper checks the pending restart job and only replaces its verified target, without sending a second signal to active work. Preserve all ledgers, credentials, artifacts, unknown fences and receipt outbox. Read back service runtime, worker capacity and original job generations after rollback.

## Rollout history

- 16:41 CST: first drain snapshot captured six prepares and one publish. Color review rejected the initial packed-RGB prototype before activation; the verified old release was restored as the pending target.
- 16:52:15 CST: color-qualified ec898477 queued, then active16:53:15. Backup085215Z.
- 17:06:05 CST: heartbeat reconfirmation and shared fitting queued; 7c6866d active17:20:04. Backup090605Z.
- 17:22:14 CST: final exact native-pixel shortcut53e7016 queued. Backup092214Z. All remaining work drained before final activation17:37:33.

No individual FFmpeg or platform task was killed; historical unknown publications were not retried.
