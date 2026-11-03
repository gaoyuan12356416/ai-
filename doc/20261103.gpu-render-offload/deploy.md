# Deployment and rollback

GPU host 43.154.250.89. Runtime candidate ec8984773c4bc33e832ec5ae483cb3dcad7482dc; helper 449b5cce37516cde301bbee3642d4f1253ee8616. Both pushed to GitHub and fetched as clean immutable worktrees. Runtime path /opt/pipeline-dispatch/releases/ec8984773c4bc33e832ec5ae483cb3dcad7482dc. Flag RANDOM_GPU_PIPELINE_V2=1 is in the staged private environment and installation manifest.

Capacity readback v81: GPU 6/4, CPU 4/6, global10; all manual. No capacity or API process changes.

16:41 CST: first natural-drain request; snapshot captured 6 prepares and 1 publish. Pending prototype target was held during color review, and old verified code restarted after original work drained.
16:52:15 CST: final verified release queued; two already-claimed publications continue naturally before new code starts. No new signal or mutation to individual FFmpeg/platform tasks.

Final switch backup: /data/pipeline-dispatch/maintenance/gpu-offload-20261103T085215Z. Earlier snapshot: /data/pipeline-dispatch/maintenance/gpu-offload-20261103T084113Z. Both retain private manifest, unit and environment.

Rollback uses the same audited drain, preserves all ledger/artifact/credentials/outbox data, and never resets a job. On GPU:
```sh
/opt/pipeline-dispatch/runtime/python/bin/python3.10 /opt/pipeline-dispatch/releases/449b5cce37516cde301bbee3642d4f1253ee8616/scripts/activate_pipeline_gpu_offload.py --current /root/pipeline-node-gpu.json --candidate /data/pipeline-dispatch/maintenance/gpu-offload-20261103T085215Z/install-manifest.before.json --apply
```

While already draining, add --replace-pending; this verifies the pending restart job and changes its verified target without signaling active jobs again.
