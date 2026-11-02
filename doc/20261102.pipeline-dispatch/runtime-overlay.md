# Runtime overlay helper

`scripts/deploy_pipeline_runtime.py` is a local server-side deployment helper.
It does not SSH, stop/start services, change environment files, enable workers,
or restore publication ledgers. The cutover owner must freeze admission, drain
the lane, and stop its named services before applying it.

Prepare from the exact clean commit already pulled from GitHub:

```sh
python scripts/deploy_pipeline_runtime.py prepare \
  --source /path/to/github-checkout --commit <commit> --base 6700188 \
  --baseline /private/runtime-baseline.json --folder /path/to/new-stage
```

The baseline format is the captured map of `lane/relative/path` to
`runtime_path`, `sha256` (or `missing: true`). Every live hash is verified before
and after preparing candidates. Symlink-based sidecars copy their entire live
release, retaining private file ownership and modes. The app always stages
only changed files, including when its root is a symlink to a data disk: its
databases and material directories are never copied and its root link is never
replaced. Other directory-based runtimes also use file overlays. The stage
path must remain available after a sidecar release cutover.

The app's reviewed `app.py`, `static/index.html`, `static/quick-nav.js`, and
`scripts/youtube_auto_publish_worker.py` are copied exactly. The worker was
captured from its existing production source before adding planner mode.
Other existing files receive only the Git diff from the base to
the selected commit; a conflict stops preparation. The new dispatch package
and monitor assets are added. The legacy FB service's GPU prepare worker is
deliberately excluded; node releases own that new implementation.

Sidecar candidates live under `/mnt/data-disk/pipeline-dispatch-runtime`, whose
parents allow the existing service identities to traverse them. Coordinator
state and deployment manifests/backups retain private permissions. For an app
increment already deployed at `5de5613`, use that commit as patch base and
include every existing dispatch module and monitor asset in the raw baseline.

The reviewed X file bases are pinned independently; do not change the whole
lane base. Fetch their owning origin branches before preparing a release:

| File | Reviewed GitHub commit | Exact source SHA-256 |
| --- | --- | --- |
| `features/x_posts/service.py` | `a28d8bbaee71681af50a289f15bc007081381582` | `c25e3623d67962148c39925011c51f2ee366ee3c4514b435c72a71208e9b1c35` |
| `features/x_posts/account_limits.py` | `415256ccc4b60352af3bf3fd2baa63934159dcc0` | `8ccfd03287cdb92b086de9ad827db888df02fcbb23a396f5c0886d5fc443f687` |

The first file exactly matches the captured
`20261102T113225Z-pool-index-a28d8bba` release. Relative to its previous
reviewed `415256` source, it only adds the complete material-key lookup index
and its idempotent initialization. The integrated source retains that index,
the existing account-limit/unavailable-account fixes, and dispatch admission
guards. Set each baseline entry's `patch_base` and `patch_base_sha256` to its
row above. The helper checks fetched origin ancestry and exact or normalized
source hashes, records the per-file base, and refuses any unreviewed drift.

The operator supplies a private controls file with the exact lane units and
ledger paths. For example, replacing these paths/names with verified inventory:

```json
{"x":{"units":["example.service"],"ledgers":["/path/to/canonical.sqlite3"]}}
```

After admission is frozen and in-flight work has finished, stop the named
services using the cutover controller, then:

```sh
python scripts/deploy_pipeline_runtime.py apply \
  --folder /path/to/new-stage --lane x --controls /private/controls.json
```

Apply refuses active services, baseline drift, a changed candidate or an
unexpected release. It takes SQLite online backups with `quick_check`, then
rechecks that services are stopped. A symlink-based runtime swaps its release;
a directory-based runtime atomically replaces each staged file. It leaves all
services stopped for the cutover controller's checks and startup.

Rollback uses the same stopped-service checks and requires the applied hashes:

```sh
python scripts/deploy_pipeline_runtime.py rollback \
  --folder /path/to/new-stage --lane x --controls /private/controls.json
```

Rollback restores code only. Publication receipts, lease history, holds and
deduplication records are never rolled back. A process crash during a directory
runtime's multi-file application requires operator review; it is not silently
retried over an unexpected partial state.
