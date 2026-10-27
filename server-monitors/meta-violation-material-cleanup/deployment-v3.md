# V3 idle-only deployment

Prepare an immutable GitHub commit archive under
`/mnt/data-disk/meta-violation-material-cleanup/releases/<FULL_SHA>`.
After verifying the fetched commit, write `release-proof.json` at that release's
root with `{"commit":"<FULL_SHA>","files":{"relative/path":"sha256"}}`.
Include every deployed source file; the deployer requires all critical execution,
policy, reporting, scanner and shared-ledger modules. File hashes and the literal
`20261027-v3` policy contract are checked without importing candidate code.

Invoke the candidate release's `deploy_when_idle.py` with
`--candidate-release <absolute-release> --expected-current <absolute-old-release>
--expected-sha <FULL_SHA> --config /mnt/data-disk/meta-violation-material-cleanup/config.json`.
The default is read-only preflight. Add `--apply` for the approved cutover.
Exit `75` means a worker or lock is busy: an independent five-minute deployment
timer may retry. Exit `1` requires examination; do not force the switch or clear
claims. Exit `0` reports `ready`, `applied`, or `already_applied`.

Apply requires the mounted data disk UUID, at least 1 GiB free, exact old/current
release, no live runtime/shared owner, no unresolved active claim, and the
nonblocking `process.lock`. Unknown deletion fences are retained. It never stops,
starts or restarts the cleanup service. Only after idle verification does it
briefly stop the cleanup timer, back up config and unit metadata, then atomically
replace config and the release link while holding the worker lock. Only
`lookback_days=3` and `shared_reference_scan_workers=4` are changed.

The previous timer state is restored **after releasing the worker lock**, so a
persistent scheduled activation cannot fail by racing the deployment's lock.
The durable intent is `deployments/<FULL_SHA>.json`, written with the original
timer state and verified config backup **before stopping the timer**. Reruns
resume a prepared cutover only if the config hash is exactly the saved old or
new value and the link is exactly the saved old or candidate release. This covers
crashes after timer stop, config replacement or release-link replacement.
Applied reruns only finish a pending timer restore; they do not switch again.
An unexpected config/link or `needs_attention` receipt requires manual
reconciliation. Never delete the receipt to force a retry.

No extra cleanup run is launched. Verify the next normal 09:00/21:00 Beijing
batch's policy, source counts, exact deletion proofs, reference scan peak and
timings. Do not compare total runtime without accounting for changed scope.

For performance rollback, retain the V3 release and 5-plus-paused policy and set
`shared_reference_scan_workers=1` only in a verified idle gap. For a serious
failure, stop future scheduling and preserve current ledgers and artifacts.
Never re-enable the old active-Ad deletion policy or restore old SQLite files.
