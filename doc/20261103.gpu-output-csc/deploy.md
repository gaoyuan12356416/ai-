# Deployment

Target43.154.250.89, pipeline-node-hk-gpu.service. Exact GitHub candidate306b1673af29b1903987f23b906467ce09b871f0 at /opt/pipeline-dispatch/releases/306b1673af29b1903987f23b906467ce09b871f0. Previous runtime53e7016651b8e260d9b646b1fb82c03709a53c5a. Source code is clean and immutable. The candidate private manifest was copied from current with only SHA changed. Existing RANDOM_GPU_PIPELINE_V2=1 remains enabled.

Actual service-user installer verification completed successfully. At2026-11-03 18:12:45CST the audited helper queued natural drain and backed up manifest/unit/environment to:

/data/pipeline-dispatch/maintenance/gpu-offload-20261103T101245Z

Old supervisor PID220359 finished all eight active preparations and both active publications naturally. New supervisor PID241865 started at2026-11-03 18:24:00CST. Exact runtime and feature flag were verified through /proc. No per-job kill, platform retry or ledger reset. CPU services are untouched. Pre-deploy central capacityv89 is GPU8/4, CPU8/6, global10; frontend updates remain authoritative. This supersedes the earlier observed6/4 and4/6 counts.

## Exact rollback

Run on43.154.250.89 after activation:

```sh
/opt/pipeline-dispatch/runtime/python/bin/python3.10 /opt/pipeline-dispatch/releases/306b1673af29b1903987f23b906467ce09b871f0/scripts/activate_pipeline_gpu_offload.py --current /root/pipeline-node-gpu.json --candidate /data/pipeline-dispatch/maintenance/gpu-offload-20261103T101245Z/install-manifest.before.json --apply
```

This reuses the audited drain and restores53e7016. Preserve all capacities, ledgers, artifacts, credentials and receipts. Do not send an additional restart while the current natural drain is in progress; the command above is for the active final release.

Final readback configurationv91 preserves GPU8/4, CPU8/6 and global10; live worker counts match. The latest configuration disables X (enabled_channels=[fb,tt,youtube]), so no new production X render entered the release during acceptance. Preserve this saved switch. Actual-device media tests passed; new production CSC/artifact acceptance remains unobserved until normal eligible tasks arrive. Before-snapshot tasks all reached terminal success at unchanged generations. See test-report.md for precise evidence and this remaining acceptance limit.
