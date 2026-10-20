# Backend implementation and offline evidence

Implemented modules: `core.py`, `guards.py`, `service.py`, `worker.py`; additive
SQLite schema and triggers; existing sidecar API mount and runtime integration;
old automatic selection/claim and operator CLI cross-family guards; CPU GPU
adapter recipe retention; independent worker/service/timer and canary script.
No GPU service source is included in this change.

## Checks run

- `python scripts/test_fb_video_publish_core.py`: **41 tests passed** (39-test
  pre-review suite plus two actual-interval regressions). Covers
  real loopback handler authentication, read-only macro/attribution previews,
  maximum short-link budget, missing macro data per pair, atomic config/CAS and
  batch failure, concurrency/idempotency, tenant/owner scope, explicit repeat,
  task pagination, independent Beijing-date quota, frozen history, stale callback
  cancellation, combined old/new GPU capacity and Page execution, durable
  attempt before Graph, credential-only rotation, unknown holds and restart.
  Includes exact 4 MiB request acceptance / one-byte overflow rejection, opaque
  canary content IDs, and expired preparation/link callback rejection before a
  replacement worker has claimed the task.
  Delayed successful predecessors now impose the actual configured interval;
  failed predecessors do not impose an additional success interval, while the
  original plan time and reserved quota day remain unchanged.
- Existing automatic suite: 152 ran, 151 passed; one pre-existing failure
  `test_fb_auto_deploy.DeployContractTests.test_units_use_isolated_user_env_and_release`.
  It asserts every old service uses `fb-auto-post`, but the committed
  `fb-auto-post-effects.service` already uses root for its SQL FIFO socket.
  This unit is not modified by this feature.
- Existing material/manual suites: **39 passed**.
- Existing pool extension suite: **19 passed**.
- Existing GPU suites: **17 passed**.
- `build_runtime` prebuild/default/capacity regression passes with the new
  broker adapter; no source query runs during runtime construction.
- After the final broker and worker changes, targeted automatic
  v2/service/store/publisher regression: **94 tests passed**.
- Python compile and `git diff --check` passed on the owned changes.

All platform/GPU/source calls in these tests are fakes; the loopback HTTP test
serves only an isolated temporary SQLite store. No production task is submitted.

## Baseline failure independently reproduced

Read exact blobs from `41b5ca4f1e8f64945f56b2482e8e3dd96e2c9e89` into a
`TemporaryDirectory(prefix="fb-video-baseline-")`: the baseline
`scripts/test_fb_auto_deploy.py` and all nine baseline
`deploy/fb-auto-post-*.service` files. No file in the shared checkout was replaced.
The exact test process was:

```text
C:\Python314\python.exe C:\Users\gaoyu\AppData\Local\Temp\fb-video-baseline-d4lcltff\scripts\test_fb_auto_deploy.py DeployContractTests.test_units_use_isolated_user_env_and_release
```

Result: exit **1**, one test run, the same line 19 assertion:
`AssertionError: 'User=fb-auto-post' not found`, against the effects unit whose
baseline content says `User=root` and documents the root-only SQL FIFO socket.
The temporary directory was cleaned automatically after collecting the result.

The source acquisition commands were
`git ls-tree -r --name-only 41b5ca4f1e8f64945f56b2482e8e3dd96e2c9e89 deploy`
and `git show <baseline>:<each relative file>`.
The following exact diff command returned no output:

```text
git diff 41b5ca4f1e8f64945f56b2482e8e3dd96e2c9e89 -- scripts/test_fb_auto_deploy.py deploy/fb-auto-post-effects.service
```

Exact baseline blob SHA256:

- Test: `8d7a04a73bb7e5a9b171013d7bad9846fe1f978dac16fcc665ec3ec2a96f14dd`
- Effects unit: `38629be78608c75d61e3623740049d7e411070403d69e88c450e875e926300f4`

## Final state-machine and scheduling review scope

The review checked whole-batch reservations under `BEGIN IMMEDIATE`, quota
aggregation for every planned Beijing day before inserting any batch/link ID,
same-operation recovery, predecessor ordering, global Page/unknown fences, lease
completion, and the Meta intent/receipt boundary. No unresolved P1 was found.

Quota is explicitly a **planned-day reservation**, counting non-cancelled manual
tasks independently of automatic quota. It is not an actual-send-day rate limit:
cross-day backlog can execute on a later day without changing the reservation.
Batch ordering and the added actual-success interval apply within the same batch
and Page. Across different batches and automatic jobs, only global mutual
exclusion/unknown protection is promised; no cross-batch FIFO is imposed.

## Prepare-only canary prepared, not executed here

`scripts/validate_fb_video_prepare_only.py` defaults to dry run and requires a
reviewed allowed video URL, content ID and an absolute isolated evidence folder.
With `--execute`, it makes exactly one stable prepare-only GPU job, saves an
independent SQLite ledger and `evidence.json`, and asserts zero Meta attempts and
zero links. It rejects production state/link directories and a database containing
automatic tasks or unrelated manual tasks. It does not publish to Facebook.

The deployment owner must verify the returned output URL, SHA256, profile,
duration and recipe through this canary before declaring GPU integration verified.
The CLI dry run was exercised with opaque content ID `dz7pZsXfV0`; `execute:false`
was confirmed. No evidence database or external request is created in dry run.
