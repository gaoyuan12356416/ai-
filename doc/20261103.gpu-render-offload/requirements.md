# GPU source-stage offload

Keep GPU prepare/publish 6/4, CPU 4/6, global publish 10. Frontend capacity stays authoritative; save affects new claims, decreases drain active jobs.

Final implementation: NVDEC H264/HEVC decoding; OpenCL Lanczos cover/contain scaling and source YUV/RGB conversion; unchanged OpenCL composition and NVENC encoder. Retain the final CPU RGB/YUV conversion to preserve delivery color descriptors: deployed FFmpeg 7.1 forces BT.601 for NVENC packed-RGB input. No encoder/driver upgrade. Audio, frozen recipe, delivery profile, artifact recovery and publication fencing remain owned by existing code.

Admission is explicit: SDR limited-range 8-bit YUV420 H264/HEVC, square pixels, stable keyframe geometry, 20–60 fps and no material rate/start-time irregularity. Full-range, HDR, interlaced, low/VFR, delayed-start or changing-resolution inputs retain the established compatibility renderer. Keyframe-only scan is bounded to 45 seconds and cached by source path/size/mtime. No fallback after a selected GPU command fails. No YouTube changes.
