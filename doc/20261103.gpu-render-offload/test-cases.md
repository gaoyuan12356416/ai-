# Tests
- Qualification: unsupported codec, bit depth, HDR, interlace and geometry stay on legacy path.
- Same frozen source/recipe: frame count, duration, output pixel format, SSIM/PSNR/VMAF, visual comparison and audio PCM hash.
- Portrait, landscape, scaled input, HEVC, VFR/delayed timestamps and changing dimensions.
- GPU decoder/compute/encoder activity and per-job CPU seconds.
- Frontend raise/lower both stage pools; authority version checks, natural draining and no in-flight termination.
- Existing artifact recovery, publish intent/receipt idempotency regression.
- Real due job after deployment, no test posts.
