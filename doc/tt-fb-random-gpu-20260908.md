# TT / FB random overlay CPU reduction, deployed 2026-09-08

Both Hong Kong random workers now consume shared, predecoded lossless RGBA NUT
layers. OpenCL still performs the composition and NVENC still encodes the output.
The source decoder, dynamic cover/contain geometry, encoder settings, audio,
recipe, profile, and historical manifests remain unchanged.

## Evidence and decision

Same 60-second source and deterministic recipe, serial local renders on the
Hong Kong T4. These are render-stage measurements, not platform publication or
end-to-end latency guarantees. CPU seconds are summed process CPU time.

| TT run | Wall seconds | CPU seconds |
| --- | ---: | ---: |
| Current fused baseline, this run | 41.57 | 184.23 |
| NVDEC only | 43.30 | 186.35 |
| NVDEC and CUDA source scaling | 44.02 | 185.48 |
| Cached transparent VP9 layers | 22.84 | 105.29 |
| Cached transparent and static PNG layers, prototype | 20.56 | 58.71 |
| Final deployed code and verified cache | 24.89 | 63.49 |

Final TT CPU computation falls about 65.5%; final wall time about 40.1% against
this run's baseline. Warm cache, host scheduling and I/O affect elapsed time.
NVDEC/CUDA source options were not deployed because they did not improve this
720x1280 sample. The accepted optimization removes repeated PNG/alpha-VP9 decode
and repeated RGBA conversion, then supplies ready RGBA frames to the GPU.

Final FB render: 24.11 seconds, 64.49 CPU seconds. The final 60-second TT and FB
files are byte-identical to their respective prior fused outputs:

- TT: `f03c9e180f4980945787882c3637eafbcb9ae6d831b3769d652515d1d5c54d40`
- FB: `0a220f8678391b4c386808a549996a7bbd4200c6d0c22effd556d35b32614104`

Both have 1800 frames and decode successfully. TT local tests: 102 pass; FB:
23 pass. Each platform also passes four real GPU frame-identity regressions:
portrait, landscape 25fps, in-stream resolution change, delayed video versus
audio. All cached RGBA frames, alpha bytes, and timestamps match decoded source
assets. No test Post or worker job was submitted.

## Storage and operation

`RANDOM_GPU_ASSET_CACHE_ROOT=/data/random-overlay-gpu/asset-cache-v1` is installed
in both workers' `80-asset-cache.conf`. The 18 shared content-addressed layers
occupy 27,028,955,334 bytes (25.17 GiB). TT and FB asset sets deduplicate fully.
The builder limits the cache to 96 GiB, each build to 8 GiB, and reserves 32 GiB
free capacity; verification rejects truncated output. Cache construction is
offline and serialized. It never happens inside a production rendering request.

Never replace cached files in place or automatically delete active cache files.
Build new asset versions with `scripts/build_random_gpu_asset_cache.py` before
activating their asset manifests. Enabled cache misses, unverified receipts or
changed file metadata fail closed rather than silently restoring CPU overload.
Whole-file SHA-256 is checked during build/reuse; runtime checks content identity
of the source plus cache receipt/size/mtime and does not reread multi-GiB raw
files merely to hash them for each job. Disabling the environment setting and
restarting the two drained workers is an explicit rollback option.

## Production and rollback

Host `43.154.250.89`:

- TT runtime `/data/tt-post-gpu/releases/18fc113`, commit
  `16ae57a00cc9889c95b9b84d5475db10995da08d`.
- FB runtime `/data/random-overlay-gpu/releases/984d663`, commit
  `d2f85b6aa5c77c150886b6254019f4ad65007d92`.
- Backup/acceptance: `/data/random-overlay-gpu/backups/20260908-cache/`.
- Benchmark files: `/data/random-overlay-gpu/benchmarks/verified-cache-tt/` and
  `/data/random-overlay-gpu/benchmarks/verified-cache-fb/`.
- Existing CPUQuota=300% and Nice=5 are preserved for each random worker.

At cutover: TT manifests 953, TT publish-ledger files 893, FB manifests 962;
counts and aggregate SHA-256 unchanged. Direct-outro and X worker PIDs unchanged.
Both random workers and their reverse tunnels are active. Both GPU localhost
and CPU localhost 18830/18836 health checks pass. All seven TT triggers and FB
prepare.timer restored; maintenance journal restored=true and gates empty.
No immediate natural new-job completion was required or claimed for acceptance.

Rollback procedure:

1. On CPU `43.166.187.96`, use
   `/mnt/data-disk/random-overlay-gpu/backups/20260908-cache/maintenance.py`
   with `gate-on tt --apply` and `pause tt --apply`; save/stop
   `fb-auto-post-prepare.timer`. Drain random workers without killing renders.
2. On Hong Kong run
   `bash /data/random-overlay-gpu/backups/20260908-cache/rollback.sh`.
   This restores old code pointers and removes only the new cache drop-ins,
   then starts the workers and their two reverse tunnels. Keep data/ledgers.
3. Verify GPU 8830/8836 and CPU 18830/18836 health. Use the same controller with
   `resume tt --apply` and `gate-off tt --apply`, then restore the FB timer to
   its saved state. Confirm all original triggers and restored=true.
