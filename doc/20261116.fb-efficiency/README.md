# FB first-round efficiency experiment

Scope: cross-job original-source caching, an encoding comparison, and an isolated
concurrency/throughput matrix. No publication or historical replay is part of the
experiment. Production receives first use of GPU and preparation resources.

## Source cache

`features/fb_gpu/source_cache.py` keys by the exact source URL's SHA256. A cache
hit requires a successful conditional GET (304) against a strong ETag, matching
size and a verified local SHA256. A changed source produces a new body and hash;
existing frozen recipes retain their original source-conflict check. Sources
without a strong validator use an ordinary download. Jobs get separate hard
links, or a copy across filesystems, and retain independent recipes and outputs.

The 32 GiB cache stays under the existing work root. It preserves a 40 GiB disk
reserve and evicts only its own idle blobs. Cache-storage failures degrade to a
regular download. Per-job `source-cache.json` records hit, body bytes, validation
status and time without saving the URL or credentials. Completed-job manifests
include that observation. Existing manifests remain compatible.

## Measurement

`scripts/benchmark_fb_efficiency.py` runs native media processing against copied
job metadata and existing local source bytes. It does not import publishing
adapters or upload outputs. Encoder variants keep the source, recipe, dimensions,
frame rate and duration identical. Throughput is measured as completed media
seconds per wall second at 4/8/12/16/24 simultaneous tasks. An external expiring
permit and a local production-media watchdog cancel only benchmark-owned process
groups if production needs the device. Incomplete rounds are reported separately.

Daily-capacity projections must distinguish rendering capacity, preparation
overhead and the publishing bottleneck. Sample-length extrapolations and a full
24-hour production result must be labeled separately. No encoder or concurrency
change is automatically promoted from an isolated benchmark.

## Validation and rollout

Run the relevant source-cache tests on Windows and Linux. Stage the exact pushed
GitHub revision on the GPU, preserving current runtime overlays. Back up affected
code before any activation. The cache integration may be installed with a scoped,
hash-verified patch; existing preparers finish their current claims before natural
replacement. Never restart publisher workers or change scheduler/capacity settings
as part of the cache rollout. Rollback restores the backed-up preparer entry file,
retains compatible additive cache code and current business facts, and naturally
replaces preparation children only.

## Startup catalog admission

Replacing the idle GPU preparers exposed repeated sequential CUDA SHA256 reads
of the same full template catalog before worker registration. Catalog admission
now explicitly uses standard host SHA256 and the existing per-process immutable
file cache. All expected SHA, size, inode, mtime/ctime, replacement and symlink
checks remain. Requested media/download/output GPU hashing retains its original
route and failure behavior. No rendering, audio or recipe setting changes.

The helper has an independent hash-verified backup/rollback action
`catalog-admission` in the installer. Only unregistered startup processes proven
to hold zero business claims may be replaced to finish this activation. Active
business preparation and all publisher processes must continue normally.

## First-round result

See `results.json` for the dated counts, formulas and verification. With identical
24-way120-second workloads, P4/CQ23 delivered13.5% more completed media seconds
per wall second and21.6% fewer output bytes; bounded2.5M/3.5M delivered14.7%
and46.7%, respectively. The estimate of roughly106/115 extra preparations per
day assumes the prior day's GPU time share. It is not a measured publishing
gain. Production retains P5/CQ21 and the existing capacity settings; only source
caching and the catalog-admission correction are activated.

Media-only A/B used host catalog verification for every variant. This now
matches the deployed catalog fix. Source downloads, COS/Meta transfers and the
independent catalog-admission improvement are excluded from the daily estimate.
Main services and all16 publisher PIDs were preserved; GPU24 and CPU4 preparers
registered after activation, with the existing CPU in-flight job retained.
