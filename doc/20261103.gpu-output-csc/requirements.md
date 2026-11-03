# GPU output color conversion

2026-11-03 operator explicitly accepts color differences and requests removing the remaining CPU output conversion. This supersedes the color-preservation requirement in ../20261103.gpu-render-offload/requirements.md.

Feed OpenCL-composed RGBA directly to NVENC with -pix_fmt rgba -rgb_mode yuv420. NVENC converts to encoded YUV420 on device. Keep output dimensions, timing, audio, encoding quality settings and frozen recipe unchanged. Apply the same GPU output path to compatibility-source renders while keeping their source admission/normalization safeguards. Preserve the latest saved pools and frontend dynamic capacity. Live pre-deploy configuration v89 is GPU prepare8/publish4, CPU prepare8/publish6, global10; the operator changed these through the frontend during validation. No in-flight cancellation, artifact recreation, ledger changes, platform replay or YouTube changes. CPU audio, IO and transfers remain necessary.
