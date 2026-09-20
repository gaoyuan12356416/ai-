# FB source-video overlay implementation

Date: 2026-09-20. Base: `e3bef98`. Branch: `codex/random-source-overlay-fb-20260920`.

## Behavior and compatibility

- Every newly derived FB recipe includes exactly `source_overlay={version:1, opacity_bp:200..500, scale_bp:11000..15000}`. All three values are strict integers; bool, null, partial/extra keys and out-of-range values fail validation.
- Draws use the existing stable identity and separate `source-overlay-opacity` / `source-overlay-scale` labels. Existing transform/asset draws, recipe version `1`, and profile `tt-post-random-overlay-h264-720x1280-v3` remain unchanged.
- An absent `source_overlay` field selects the existing legacy graph. A present invalid field never falls back to legacy rendering. The legacy CPU graph SHA-256 for the fixed regression fixture remains `13f195cc37d7d78bac9e8eb8c4761fcb3a5bfdacdc27218840dc82674aa5ddd2`.
- The new CPU graph splits the same source video timeline before template composition, aspect-covers and crops to 720x1280, enlarges and centers the unrotated source, applies alpha, and overlays it after corners. Input count, audio mapping, encoding profile and explicit source-duration `-t` bound remain unchanged.
- Shared OpenCL code, copied unchanged from the TT worktree after its owner's ready notice, samples the existing source plane after corners. The compiled-kernel filename hashes the shader source, including both new parameters. COS continues to use the final video content hash.
- `GET /health` now reports `source_overlay_version=1`.

## Durable retries and cleanup

Before the first probe/render, a successful source download is pinned in `jobs/<job_id>/recipe.json`:

```json
{
  "version": 1,
  "request": {"job_id": "...", "content_id": "...", "source_url": "..."},
  "source_sha256": "...",
  "asset_root": "/verified/immutable/catalog/root",
  "recipe": {"asset_set_sha256": "...", "source_overlay": {"version": 1, "opacity_bp": 200, "scale_bp": 11000}}
}
```

The actual `recipe` contains the entire existing recipe plus the new optional field. The file is flushed/fsynced and atomically replaced; the directory is fsynced on POSIX. Write failure prevents rendering. A retry must match request and source SHA, validates the frozen recipe, and loads the original verified catalog when the current default fingerprint differs. Corrupt/missing historical catalogs and changed source bytes fail closed without drawing again.

Completed `manifest.json` remains the first authoritative reuse check, unchanged and byte-preserved. Frozen legacy recipes remain legacy. A failed historical job with no recipe receipt has no previously frozen recipe and receives a new recipe on its first successful post-release preparation.

Cleanup retains `recipe.json`; it removes only known media/temp/kernel filenames from frozen failed jobs and does not follow symlinks. Other files and completed job directories are retained. A later retry may redownload media but cannot accept a different source hash. Immutable historical asset directories must remain available for these retries.

## Validation

Executed on Windows with Python 3.14 and FFmpeg 8.0.1:

```text
python -m unittest scripts.test_fb_source_overlay scripts.test_fb_gpu_prepare_worker scripts.test_random_gpu_compositor scripts.test_random_gpu_asset_cache
38 tests passed (the original 37 plus the explicit output-duration contract)

python -m scripts.verify_fb_source_overlay_ffmpeg
ok=true; 4 actual renders passed

python -m compileall -q features/fb_gpu features/random_gpu scripts/test_fb_source_overlay.py scripts/verify_fb_source_overlay_ffmpeg.py
passed

git diff --check
passed
```

The FFmpeg fixture substitutes libx264 only for NVENC encoder-specific options; it preserves the actual filter graph, inputs, audio mapping and duration. It checks legacy/new rendering with both a source audio track and no source audio. A wide synthetic source switches red to green and includes narrow blue stripes; opaque white corners ensure that only a topmost, synchronous, centered, unrotated source overlay can produce the expected pixels.

| Assertion | Actual result |
| --- | --- |
| Output dimensions, time, video frames | 720x1280, 1.000000 seconds, 30 frames on all four renders |
| Audio streams | Exactly one on all renders; silent sources remain silent |
| Decoded audio before/after overlay | Identical SHA-256 for each source-audio condition |
| Legacy corners | RGB `(255,255,255)` at all inspected positions |
| Enabled temporal center | RGB `(254,240,240)` then `(241,254,240)` |
| Enabled source-stripe positions | RGB `(242,242,254)` at x=60/660, y=64/1216 |
| Retry after upload failure, cleanup and catalog change | Exact frozen recipe/receipt and same FFmpeg command reused |
| Changed source, invalid recipe/catalog, failed fsync | No new render/upload |

## FB padded-audio stall correction

The FB command now omits only the output-level `-shortest` option. The validated source duration still supplies the exact `-t` value, and `aresample=48000:async=1:first_pts=0,apad` continues to fill short or missing audio. Video filter `shortest=1` / `eof_action`, input looping, audio mapping, H264/NVENC encoding parameters and recipe semantics are unchanged. This addresses the parent task's reproduced early stall with padded audio; full-length production-source acceptance remains with that task.

The regression test checks the `108.300000` second bound across CPU/OpenCL, legacy/new recipes and source-audio/silent inputs. After this change, all 38 related tests and the four actual CPU fixture renders passed again: each output remains 1.000000 seconds, 30 video frames and one audio stream; decoded audio is identical between legacy/new overlay variants.

## Integration and remaining acceptance

Drama's older FB adapter needs only the updated `random_overlay.py`, shared `validate_source_overlay`, and the new-layer section in `prepare_worker.build_command`. The FB-only receipt, cleanup and health changes must not replace unrelated older Drama worker code.

The CPU path follows the established even-dimension scale rounding; arbitrary basis-point values can differ by less than two pixels in scaled extent from continuous OpenCL sampling. Both follow the approved aspect-cover, centered 110–150% semantics. Local verification does not claim production NVENC/GPU or live health acceptance. This worktree performed no server writes, deployment, upload or publishing; production checks remain with the root task after workers drain.
