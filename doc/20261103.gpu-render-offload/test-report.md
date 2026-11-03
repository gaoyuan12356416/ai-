# Validation report — 2026-11-03

Final runtime **53e7016651b8e260d9b646b1fb82c03709a53c5a** is active on Hong Kong GPU. Normal production task 5553 completed preparation and publication on this release. Existing unknown uploads remain held; they are excluded from a claim of all-task success.

## Fixed-input media validation

An immutable 811.6-second real source, frozen recipe/assets and unchanged encoder/audio settings were used. The first 20 seconds contain 600 output frames. Compare the previous cache-enabled renderer against final GPU source offload.

| Sequential paired sample on the same loaded host | Previous | Final |
|---|---:|---:|
| Render CPU seconds | 29.075 | 25.807 |
| Render wall seconds | 18.028 | 19.676 |
| Output frames / duration | 600 / 20 s | 600 / 20 s |

Render CPU reduced **11.2%**. This pair does not demonstrate improved wall throughput. One-time keyframe admission of the entire 811.6-second source added 3.311 CPU / 6.877 wall seconds, excluded from render timing; it is not repeated per 20-second segment. Shared-host measurements do not establish a fleet-wide percentage improvement.

Final quality: SSIM **0.992094**, YUV PSNR **45.793463 dB**, VMAF mean **97.63242** (minimum 95.389435), decoded-RGB PSNR **41.172033 dB**. Source/output matrix, primaries and transfer are BT.709, limited-range YUV420. Visual side-by-side inspection found consistent composition, text, asset positions and colors.

1080p portrait, landscape and HEVC scaling/decode samples measured SSIM 0.992253, 0.992578 and 0.992240; each has 180 frames / 6 seconds and identical audio PCM to its old-renderer counterpart. Full-range and delayed low/VFR inputs retain the old renderer with byte-identical output. Actual keyframe inspection rejects changing-resolution input before selecting CUVID.

Flash/beep synchronization: both outputs contain 90 frames; video peak and audio peak are both at frame 30. Audio PCM SHA256 is equal: e180ecdbd542b9ef2ef15a375c5658a619a0356fc9d3c9ce9666b98ef0c84b2c.

Identical cover/contain geometry now shares one GPU fitting result. Native integer Lanczos sample locations use the exact center sample. Both optimizations produced byte-identical output to the color-qualified version: ea215f73bcbffa2ff46aa8afbe2ad5548da3fc78553185aed21a562b49931183.

NVDEC decode, OpenCL source scaling and source YUV-to-RGB conversion are offloaded. Final RGB-to-YUV conversion deliberately remains on CPU: FFmpeg 7.1 packed-RGB NVENC forces BT.601, violating the previous color contract. CPU work is reduced, not eliminated.

## Code, capacity and UI

Final regression set: **105 tests passed**, covering renderer admission, asset reuse, transient-heartbeat recovery, rejected-lease fencing, receipts, late artifacts, process races and lowering capacity without cancelling active jobs. Additional heartbeat/lease/receipt regressions passed 59 tests. The final exact-center kernel shortcut was exercised on the actual GPU with byte-identical output.

Real Chromium fixtures passed administrator/personal permissions, both prepare and publish capacity raises/lowers with expected_version, draining display, 15-second refresh, deep links and mobile layout. Existing frontend/API capacity behavior remains authoritative; no new hardcoded pool size was introduced.

The actual service-user installer verified encoder/compositor/source decode-fit, immutable catalog/cache and private COS roundtrip. Final installer's SSH client disconnected, but the durable .verified marker and activation helper's clean exact-SHA verification confirmed server-side completion. No platform test posts were created.

## Live readback and business acceptance

- 17:37:33 CST: final runtime became active, supervisor PID 220359, service active/running. /proc confirms the exact final release and RANDOM_GPU_PIPELINE_V2=1.
- 17:44 CST: six simultaneous real FFmpeg processes use CUDA decode, OpenCL fit_yuv_cover, cached RGBA assets and h264_nvenc; no CPU scale or libx264. Final CPU output conversion remains present as intended.
- Authority configuration **v82** and live heartbeats agree: GPU prepare **6**, publish **4**; CPU prepare **4**, publish **6**; global publish **10**. All four pools are manual. This deployment did not modify capacity.
- Final six-render 20-second sample: CPU **89.93%**, GPU compute **49.2%**, encoder **35.0%**, decoder **5.2%**, VRAM **4110 MiB**. The GPU is doing more than encoding; CPU remains heavily loaded at six simultaneous renders. This is a current-state sample, not an equal-workload fleet comparison.
- Original six prepares and original active publication completed naturally at generation 1. The six prepares captured before the heartbeat follow-up also completed at generation 1 with no error.
- GPU-produced normal task x:queue:5540 crossed COS to CPU publication: SHA d0901b1842e981f924481f3c1e2ade261556e5fe7015a478f1698f8005448018, 102131641 bytes, 173.685 seconds, frozen profile unchanged, Post ID 2117543466880106592.
- Hardened-worker publication x:queue:5547 completed during natural drain at generation 1, Post ID 2117549178855522458.
- **Final release** task x:queue:5553: prepare 32016 completed at 17:38:47 CST, publish 32041 completed at 17:39:19 CST, both generation 1. Artifact SHA d1894f544b11ca0650ed8fe10ed91c2da3ef4490131c6845a38316ee3ba80261, 7648807 bytes, 11.076 seconds; exactly one final publication record, Post ID 2117552183743000859.
- At 17:45:19 CST no job started on the final GPU release had entered failed/unknown.

## Remaining held uploads

Five old-worker publication attempts remain unknown: central IDs **31990, 31992, 31993, 31994, 31995**, X queues **5532, 5534, 5524, 5522, 5523**. All remain generation 1 / native attempt_count 1. Central operations contain initialize/append only, no finalize/create-post intent; native records have no Post ID. No automatic retry, publication replay or account-unfencing was performed. Their transient-heartbeat failure path is hardened in the final release, but historical reconciliation is still outstanding.

## Durable evidence

GPU /data/pipeline-dispatch/diagnostics/gpu-cpu-20261103/: final-runtime.json, final-live-commands.json, paired-results.json, final-quality-results.json, yuv-output-results.json, admission-results.json, matrix/final-sync-compat-results.json, matrix/final-matrix-results.json, shared-gpu-results.json, native-gpu-results.json.

CPU /mnt/data-disk/pipeline-dispatch/diagnostics/gpu-offload-20261103/: before.json, heartbeat-before.json, readback.json, held-upload-audit.json, final-audit.json.
