# Design review

Self-review: accepted. FFmpeg7.1 NVENC packed-RGB implies BT.601 conversion; explicitly authorized by the operator. Force rgb_mode=yuv420 to retain platform-compatible 8-bit 4:2:0 output, not YUV444. Preserve source hardware-admission guards and all audio/timestamp arguments. Do not change drivers, worker lifecycle or publishing code. The existing RANDOM_GPU_PIPELINE_V2 flag gates compatibility-path output acceleration; no change to CPU node behavior.
