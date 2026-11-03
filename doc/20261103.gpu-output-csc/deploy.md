# Deployment

Target Hong Kong GPU43.154.250.89, pipeline-node-hk-gpu.service. Baseline53e7016651b8e260d9b646b1fb82c03709a53c5a. Push exact GitHub candidate, create immutable /opt/pipeline-dispatch/releases/<SHA>, stage via pipeline_node_install.py --verify with a private manifest copied from the current one and only SHA changed. Existing RANDOM_GPU_PIPELINE_V2=1 stays enabled. Use activate_pipeline_gpu_offload.py --apply after media qualification; it backs up and drains active work without killing it. CPU service and front-end capacity remain unchanged. Read back original tasks, runtime, flags, worker counts and production command/output.

Rollback uses the same helper and its newly created install-manifest.before.json; no ledger/config/artifact reset. Record the actual backup, exact command and acceptance before closing.
