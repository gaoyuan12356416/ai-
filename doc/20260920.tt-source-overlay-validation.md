# TT source-overlay implementation evidence — 2026-09-20

Base: `326e16defb8f0b1aec76e8f36d52bb6359275a98`. Local implementation and validation only; no server writes, deployment, upload or platform publishing.

## Behavior

- New random recipes retain recipe version 1 and existing TT media profiles. They add strict `source_overlay={version:1, opacity_bp:200..500, scale_bp:11000..15000}` using the stable labels `source-overlay-opacity` and `source-overlay-scale`.
- Absent field preserves legacy rendering. Present null, partial/extra keys, bools, non-integers and out-of-range parameters fail closed.
- GPU reuses the existing synchronized 720×1280 aspect-cover source plane, applies an unrotated centered zoom, and blends it after corners. No extra source input/decode or audio stream. Shader cache identity includes the parameters. Startup preflight compiles the enabled branch.
- TT CPU rendering splits the original source before transforms, normalizes aspect-cover, zooms/crops, and blends last. Missing-field commands are byte-identical to the base implementation.
- New TT recipes are fsynced to `work_root/recipes/<job_id>.json` before download/render. Source SHA/size are pinned after download and before probing/FFmpeg. Restart retries reuse the receipt and reverify its original catalog/root, including after a default catalog change. Scratch and terminal-media cleanup do not remove these receipts.
- Completed legacy manifests remain authoritative and unchanged. Corrupt manifests/receipts and changed source/request/catalog bytes fail closed. Completed newer manifests may resolve a historical catalog through their matching immutable local receipt.
- Random-worker health advertises `source_overlay_version=1`; direct-outro profiles and publishing gates remain unchanged.

## Automated checks

```text
python -m unittest scripts.test_tt_random_overlay scripts.test_tt_random_catalog_rollover scripts.test_random_gpu_compositor scripts.test_random_gpu_asset_cache scripts.test_tt_gpu_worker
Ran 125 tests in 5.716s — OK

python -m py_compile features/tt_gpu/random_overlay.py features/tt_gpu/worker.py features/random_gpu/compositor.py scripts/test_tt_random_overlay.py scripts/test_tt_random_catalog_rollover.py scripts/test_random_gpu_compositor.py scripts/check_random_gpu_timeline.py
Exit 0

git diff --check
No whitespace errors
```

Coverage includes deterministic and independent choices; all four parameter endpoints; strict malformed-value rejection; legacy completed/frozen recipe preservation; download/transcode failure retries; catalog/default/release changes; source mismatch; corrupt receipts; cleanup retention; graph/audio/delivery preservation; source-plane reuse; shader cache separation; and enabled-branch startup preflight.

The first suite run exposed a Windows-only assumption in the new cleanup test: existing `chmod(0400)` scratch assets can survive `rmtree(ignore_errors=True)` on Windows. The test now verifies cleanup invocation/scope and receipt retention instead of assuming POSIX unlink behavior. No production cleanup behavior was changed.

## Real local FFmpeg/OpenCL checks

Intel Iris Xe OpenCL compiled and executed the shared kernel against three synthetic source frames and opaque corner assets. The current missing-field kernel output was byte-identical to the base kernel:

```text
SHA-256 6f7ca1f0b8cbc2fc7d137333b0c0054d5ea245bb37db92839a38f805e5c2dac4
```

Three enabled cases were compared numerically with same-frame, centered bilinear source sampling followed by the expected topmost alpha blend. Each checked 90 RGB channels across three frames.

| Opacity bp | Scale bp | Maximum channel error (8-bit) | Result |
| --- | --- | --- | --- |
| 200 | 11000 | 0.2600 | Pass |
| 500 | 15000 | 0.7500 | Pass |
| 347 | 12567 | 0.4753 | Pass |

TT CPU graphs also rendered four six-frame cases: old/new recipe × source with/without audio. The enabled ghost visibly changed pixels above opaque corners; missing-field commands matched the base byte-for-byte. The smoke render used raw video output, so audio mapping and delivery suffix preservation are established by the automated command-contract tests, not by a claim of encoded-audio runtime validation.

Small local receipts: `artifacts/tt-source-overlay-validation-20260920/opencl-pixels.json` and `tt-cpu-render.json`. Synthetic media lived in temporary directories and is not committed.

## Deployment acceptance and rollback notes

- No production NVIDIA/T4/NVENC check was performed here. Run enabled startup preflight and actual TT/FB encodes on the target runtime before resuming new work.
- `scripts/check_random_gpu_timeline.py --source-overlay` enables the effect at 5%/150% in the existing portrait, landscape, resolution-change and delayed-video timeline fixtures. Run that option on the target GPU in addition to existing profile/output checks.
- Preserve `recipes/`, completed manifests, publication ledgers and all referenced immutable catalog directories. Never restore old ledger snapshots or regenerate frozen recipes to repair a rollout.
- Older binaries do not understand new `source_overlay` keys or receipt-based retries. After any new recipe is admitted, rollback requires retaining a compatible reader/renderer or holding the affected unfinished jobs; blindly switching them to an older binary would violate the frozen-recipe contract.
