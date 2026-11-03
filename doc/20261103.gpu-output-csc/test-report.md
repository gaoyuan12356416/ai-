# Validation report — GPU output CSC

Active runtime: 306b1673af29b1903987f23b906467ce09b871f0 (18:24:00 CST, PID241865). User explicitly accepts color differences. Color equality with BT.709 output is no longer a gate; geometry, encoded compatibility, audio and timing remain required.

## Passed before activation

- Python compilation and 58 targeted regressions passed. GPU RGBA output applies to both qualified and compatibility-source OpenCL paths; feature-disabled/software encoder behavior remains unchanged.
- Actual service-user pipeline_node_install.py --verify completed successfully before activation, including encode/compositor/source admission, catalogs/cache and private COS roundtrips.
- Same immutable real source, recipe/assets, audio and encoding parameters; sequential paired 20-second samples on the loaded production GPU:

| Metric | Previous53e7016 | Candidate306b167 |
|---|---:|---:|
| Render CPU seconds | 27.492 | 21.535 |
| Render wall seconds | 20.999 | 15.301 |
| One-time source scan CPU seconds | 3.335 | 3.304 |
| One-time source scan wall seconds | 7.026 | 6.216 |
| Frames / video duration | 600 /20s | 600 /20s |

Render CPU reduced21.7% and sample wall time27.1%. This is one sequential pair on a shared host, not a guaranteed whole-server improvement; the operator also changed live pool capacities during this work. The source scan examines the entire811.6-second source once, excluded from render timing.

Final encoded output is720x1280 H264 YUV420 limited range, BT.601 (bt470bg) as accepted. The two outputs have identical decoded PCM SHA844ea956606d9dcf502c1afcc696713225df3961609214776163917fea9d2970. Verbose FFmpeg negotiation shows the previous RGBA-to-YUV software scaler removed. Only source NV12/YUV420 plane rearrangement remains; audio, transfers and IO still use CPU.

Actual-device portrait1080, landscape and HEVC-source cases produce180frames/6seconds. Full-range source retains compatibility source processing and now uses GPU output CSC. Low/VFR sample retains176frames/5.866667seconds video and6seconds audio, exactly matching its established baseline; do not incorrectly require180frames for that fixture. All five have identical PCM to their previous outputs and decode cleanly. Candidate H264 and HEVC outputs were both verified with ffprobe; both are YUV420.

Flash/beep tests for both H264 and HEVC output:90frames; video and audio peaks both at frame30. PCM hash e180ecdbd542b9ef2ef15a375c5658a619a0356fc9d3c9ce9666b98ef0c84b2c matches the old baseline. A real output frame was visually inspected: scene, overlays and text are present without corrupt/blank output; color differences are accepted.

## Rollout evidence

At18:12:43CST the authority configuration wasv89: GPU prepare8/publish4, CPU prepare8/publish6, global10, all manual. The actual live worker counts matched. The operator's latest saved values were preserved without a capacity write.

At18:12:45CST the helper backed up the current release and queued natural drain. Original task32057/x:queue:5563 published normally during drain (Post2117560777796354074), at unchanged generation1. All eight original preparations and both original publications completed naturally, with the same generations as the before snapshot and no error. The other original publication1122 also completed (YouTube receipt4mIYQvZ52E8); no YouTube settings or code were changed.

Evidence:
- GPU /data/pipeline-dispatch/diagnostics/gpu-cpu-20261103/output-csc/: per-case .mp4.json and verbose .log files, audio-comparison.json, frame.png.
- CPU /mnt/data-disk/pipeline-dispatch/diagnostics/gpu-output-csc-20261103/: before.json and readback.json.
- Existing five old unknown uploads were not retried or unfenced.

At18:24:44CST /proc and systemd confirmed exact release306b1673, RANDOM_GPU_PIPELINE_V2=1 and active/running. At18:24:47CST configurationv91 and live heartbeat counts remained GPU prepare8/publish4, CPU prepare8/publish6. Configurationv91 enabled_channels contains fb, tt and youtube; X is disabled. No new production X task entered this release. This setting was preserved. Therefore actual-device media validation and deployment passed, but new production render/artifact/publication acceptance for the output CSC remains unobserved until normal eligible tasks arrive. No test posts or forced queue retries were created. Do not claim post-deployment production conversion from service startup alone.

All eight candidate media samples passed packet checks: strictly increasing per-stream DTS and continuous video PTS at30fps. Evidence: output-csc/timestamp-checks.json. The original five unknown uploads remain held at generation1.

Project-specific acceptance and runtime details are recorded here; the personal skill context was intentionally left unchanged.
