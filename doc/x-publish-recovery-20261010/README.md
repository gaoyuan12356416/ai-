# X metric query limit and scheduler recovery

The X Auto metric reader expanded every candidate drama into one SQLite IN
clause. On the production SQLite 3.26 build, more than 999 bound parameters
raised `too many SQL variables` before selection, leaving tasks in retry_wait.

Read at most 400 dates and 500 content IDs per query in one read transaction.
Retain active-generation, platform, product, complete-window and content filters,
exact decimal strings, deduplicated inputs and the original global row order.
There is no publication-state migration or change to selection thresholds.

Validation:

```
python -m unittest scripts.test_x_auto_metric_query_limits scripts.test_x_auto_post_store scripts.test_x_auto_post_metrics scripts.test_x_auto_post_selector -q
```

The new tests enforce SQLite's 999-variable limit even on newer local builds,
cover 1,205 dramas, simultaneous 1,005-day / 1,105-drama filters, active-generation
selection, product/platform isolation, exact decimals, ordering and missing days.

Deployment is GitHub first. Run `scripts/install_x_auto_metric_query_limits.py`
with a full `--commit` and the exact live `--expected-release`. It overlays only
the changed core module and supporting tests/documentation on the existing
composite, drains Auto workers, backs up both databases with SQLite's online
backup API, verifies publication and token fingerprints, and restarts only the
Auto sidecar. Existing pool timer states remain unchanged during code deployment.

Rollback uses the same installer with `--rollback <backup-directory>`; it checks
the release identity and restores code only, preserving current databases and
tokens. An operational restoration of stopped pool timers is a separate action
under the user's publishing-repair authorization, following health and ledger
checks. Preserve configured accounts, frequency, frozen plans, historical failed
attempts, ambiguous outcomes and the existing 90-second schedule grace period.
Never invoke a historical catch-up or a synthetic Post to test this repair.
