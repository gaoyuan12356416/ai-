# Material drama-catalog correction

DramaWave-family material labels (Drama-comics, Drama-B/C, Drama-novel,
Drama-AI素材, Drama-社媒专用素材, DramaWave/W2A/copy variants) resolve
`ads_custom_source.data_source_id` against drama app 1479. Material IDs and
stored source products are not rewritten. Other unrelated products retain
their existing app-catalog lookup.

Mobo-family materials resolve their exact source story ID through the AI
backend Moboreels library. The library must return the same ID and a currently
listed story; an unavailable library or absent/unlisted story does not fall
back to internal app 1479/2373/3360. This selection is independent of caption
macros. Existing explicit Moboreels macros, URL generation and account ownership
remain unchanged. Frozen publish history and queued task payloads are preserved.

The runtime files are narrow deltas on independently inspected live copies.
The manifest fences each old file and runtime pointer. Tests cover the generated
catalog SQL, mixed product hydration and missing-Moboreels no-fallback behavior.

Deploy the exact pushed GitHub commit with `deploy.py SHA stage`, then for each
group `x`, `fb`, `tt`, `youtube`: `pause`, poll `busy`, `activate`, verify HTTP
readiness/source resolution, and `resume` with the returned deployment directory.
The established per-lane controls let existing work finish; do not bypass the
systemd main/X upload drain barriers. Scheduler baselines are restored after
activation. No test Post, historical replay, or metadata database write is used.

Rollback is code-only: for a resumed group run `deploy.py SHA rollback-pause
--deployment DIR --group GROUP`, wait for `busy`, then `rollback` with the same
arguments. Restore only this deployment's lane with the checked current config
version if another lane changed the revision. Never restore old publishing DBs.

Read-only acceptance: `verify_readonly.py --root LIVE_ROOT --lane LANE
--overlay SOURCE/ops/drama-catalog-20261117/runtime/LANE --material ID ...`.
After activation omit `--overlay`. Optional `--youtube-sql FILE` tests the
configured YouTube source query and the corresponding metadata resolver.
