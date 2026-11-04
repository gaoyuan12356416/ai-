# X account list language filter

The global X account list now has an "全部语言" select immediately after the
authorization-status filter. It filters the displayed accounts by the same
canonical `drama_language` used by the row editor, combined with the current
search and status. The visible count follows the filtered rows. Refreshing the
account data retains the selected language; choosing "全部语言" clears it.

The options reuse the existing language catalog and include additional language
codes found on accounts. Existing aliases (`jp` -> `ja`), case/underscore
normalization, and the default English language are unchanged.

This is a static-only change to `static/x-account-list.html`. No API, database,
account settings, publishing behavior, or service state changes are required.

## Validation

- Extracted inline JavaScript passes `node --check`; `git diff --check` passes.
- Playwright browser smoke with eight local fixture accounts covers all rows,
  Spanish, Spanish + active status, an empty search intersection, refresh
  preservation, Japanese alias, traditional Chinese normalization, default
  English, a non-catalog language, and clearing the filter.
- Visually inspect the filter bar at 1920px; existing flex wrapping handles
  smaller screens. This verification uses local fixtures, not live account writes.

## Scoped rollout and rollback

Fetch the exact pushed commit from GitHub on `43.166.187.96` and extract only
`static/x-account-list.html`. Before replacement, verify that these three files
still have SHA-256
`671eb7d890c784c65deb802e85edd4f649da2db9389207a9ea2dde9bee0737f1`:

- `/root/drama_material_service/static/x-account-list.html`
- `/usr/share/nginx/html/x-account-list.html`
- `/opt/x-post-automation/current/static/x-account-list.html`

Back up all three under `/mnt/data-disk/x-account-language-filter/`, recording
the resolved runtime target, file modes and service PIDs. Replace each file
atomically from the exact Git blob and confirm all three hashes plus public
HTTPS content match. The HTML response already uses `no-cache, no-store`; the
changed JavaScript is inline, so no external asset version changes are needed.
No service restart or reload is required.

Rollback restores only the three backed-up HTML files to their recorded resolved
paths, provided they still match this deployment's hash. Keep databases, tokens,
queues and service/timer state intact. The private deployment manifest records
the backup path and a ready-to-run `rollback.py` for this exact rollout.

Skill context is unchanged: this small list filter introduces no publishing or
operational rule.
