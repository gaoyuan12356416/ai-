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
release, retaining private file ownership and modes. Directory-based runtimes
stage only changed files. The stage path must remain available after cutover.

The app's reviewed `app.py`, `static/index.html`, and `static/quick-nav.js` are
copied exactly. Other existing files receive only the Git diff from the base to
the selected commit; a conflict stops preparation. The new dispatch package
and monitor assets are added. The legacy FB service's GPU prepare worker is
deliberately excluded; node releases own that new implementation.

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
