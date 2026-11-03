# Code review

Self-review: changes confined to shared GPU command construction, preflight and local provenance. The NVENC helper preserves unrelated output/audio/timing/quality arguments and forces YUV420. Fallback-source output acceleration is gated by existing GPU feature flag plus NVENC encoder selection. Worker, coordinator, publishers, retry logic and capacities are unchanged. Existing artifact recovery remains before rendering. Actual-device verification pending before activation.
