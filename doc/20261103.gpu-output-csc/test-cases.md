# Acceptance cases

- Both H264/HEVC NVENC accept RGBA and produce YUV420 without CPU output CSC; software encoder and feature-disabled paths retain old behavior.
- Qualified portrait/landscape/HEVC sources retain GPU source path. Full-range/low-VFR inputs retain safe source path but also use GPU output CSC.
- Fixed real source paired CPU/wall samples; count frames, output dimensions, duration and monotonic timestamps. Compare audio PCM and flash/beep synchronization; color differences are allowed.
- GPU installer preflight exercises actual service-user media and private COS; no test posts.
- Capacity, publication fencing, receipt idempotency and existing artifact recovery regressions pass. Natural drain preserves active task generations; final process arguments and real publication readback prove deployment.
