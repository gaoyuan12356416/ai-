# Development and checks

1. Centralize NVENC RGBA tail configuration and apply to both accelerated and compatibility OpenCL composition.
2. Record implementation and output conversion policy in render provenance; reuse existing recovered artifacts before rendering.
3. Run Python regressions, push exact GitHub candidate, validate actual GPU media without publishing test posts.
4. Stage with service-user installer, capture active task/capacity snapshot, deploy with audited natural drain, read back actual FFmpeg arguments and one normal business result.

Commands: python -m py_compile features/random_gpu/gpu_pipeline.py features/random_gpu/compositor.py; python -m unittest scripts.test_random_gpu_pipeline scripts.test_random_gpu_asset_cache scripts.test_x_random_template scripts.test_tt_random_overlay scripts.test_pipeline_receipt_outbox scripts.test_pipeline_late_source_artifact scripts.test_pipeline_prepare_recovery scripts.test_pipeline_dispatch_core.DispatchCoreTests.test_lower_capacity_drains_without_cancelling.
