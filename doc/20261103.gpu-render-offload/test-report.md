# Validation report — 2026-11-03

## Media
Fixed immutable 811.6-second real source, same frozen recipe/assets/audio/encoder settings; compare first 20 seconds. Current cache-enabled production renderer versus final GPU source offload:

| Metric | Current | Final |
|---|---:|---:|
| Render CPU seconds | 30.826 | 25.197 |
| Render wall seconds | 21.470 | 16.699 |
| Output frames / duration | 600 / 20 s | 600 / 20 s |

Render-only CPU reduced 18.3%, wall time 22.2%; shared-host timing is indicative, not a fleet-throughput guarantee. One-time keyframe admission scan of the entire 811.6-second source took 9.38 wall seconds under concurrent load; not included in the 20-second render timing.

Final quality: SSIM 0.992094, VMAF mean 97.63242 (minimum 95.389435), decoded-RGB PSNR 41.172033 dB. Source/output color matrix, primaries and transfer BT.709, limited-range YUV420. Visual side-by-side inspected: composition, text, asset positions and colors consistent.

1080p portrait, landscape and HEVC sources exercised GPU scaling/decode; audio PCM hashes match the corresponding old output. Full-range and delayed low/VFR sources use old renderer; output SHA256 byte-identical. Dynamic-resolution source is excluded by actual frame inspection.

Final-version flash/beep: both old/new outputs 90 frames; video peak frame 30 and audio peak frame 30; audio PCM SHA256 e180ecdbd542b9ef2ef15a375c5658a619a0356fc9d3c9ce9666b98ef0c84b2c, equal.

## Code and UI
102 relevant regression tests passed; additional existing publication-capacity drain test passed. Real Chromium fixture passed administrator/personal permissions, both stage capacity raises/lowers with expected_version, draining display, 15-second refresh and deep links. Final color change targeted 26 renderer regressions passed.

Actual service-user node installer --verify passed for ec898477, including encoder/compositor/source decode-fit checks, immutable catalog/cache verification and private COS roundtrip; no platform test posts.

## Deployment
Normal-job activation/readback in progress. Do not claim business completion from successful service startup alone.

Evidence on GPU: /data/pipeline-dispatch/diagnostics/gpu-cpu-20261103/{final-quality-results.json,yuv-output-results.json,admission-results.json,matrix/final-sync-compat-results.json}. On CPU: /mnt/data-disk/pipeline-dispatch/diagnostics/gpu-offload-20261103/before.json.
