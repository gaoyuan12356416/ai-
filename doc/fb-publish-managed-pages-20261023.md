# Video publishing picker: managed Pages only

The video publishing picker previously used `/pages?purpose=publish`, which includes
all authorized Pages and virtual version-zero settings. It now uses `/pages`, exactly
like Page management, on both initial loading and configuration refresh. Only saved,
non-deleted configurations visible to the current user appear. Authorization failures
remain visible and disabled. An empty list directs the operator to create a Page in
Page management. Existing draft selections without a current configuration retain
their warning and cannot pass the existing UI preflight guard.

This is a static-only correction. Manual/Excel API policy, accepted tasks, pending
operation reconciliation, pool scheduling, databases, and publishing services are
unchanged. All three HTML entry points version the shared JavaScript asset.

Validate with `node --check static/fb-video-publish.js`, `git diff --check`, and browser
comparison of the actual Page management IDs against publishing choices before/after
refresh. The existing manual-gates UI fixture now expects only the configured Page,
while retaining cross-language pairing and independent default text checks.

Deploy from the exact pushed branch `codex/fb-publish-managed-pages-20261023`:

```bash
python3 scripts/deploy_fb_managed_page_picker.py
python3 scripts/deploy_fb_managed_page_picker.py --apply
```

The installer verifies baseline hashes, backs up all 12 files on the data disk, preserves
file ownership/mode, installs the shared script before HTML cache versions, and records
main/FB service PIDs. No restart/reload or database backup/restore is required.
Code-only rollback, from the same release checkout:

```bash
python3 scripts/deploy_fb_managed_page_picker.py --rollback <backup>/manifest.json
```

Rollback refuses unexpected file hashes. Keep current databases, short links, and queues.

## Verified production result

Deployed 2026-10-23 17:40:46 Beijing time from GitHub commit
`39b4422767226136e54ce622b593828e756695cb`, on CPU `43.166.187.96`.
Release checkout: `/mnt/data-disk/fb-video-publish/releases/39b4422`.
Installed only the four static assets in `/usr/share/nginx/html`,
`/root/drama_material_service/static`, and `/opt/fb-auto-post/current/static`.
All 12 baseline/installed hashes passed; all four public URLs returned HTTP 200
with the exact GitHub asset hash. Main API PID 1095058 and FB sidecar PID 1181097
were unchanged. No service restart, Nginx reload, database mutation, test publication,
or historical retry was performed.

The signed-in browser originally displayed 5,661 publishing choices versus 14 saved
Pages. After release and after clicking refresh, all 14 publishing IDs exactly matched
Page management. One unavailable saved Page remained visibly disabled. The page error
area was empty. Existing extension message-channel errors in the console predated
this deployment; no claim is made about unrelated browser extensions.
Local JS/Python syntax checks and `git diff --check` passed. The existing browser fixture
was updated and syntax-checked; behavior acceptance used the real signed-in page.
Screenshot: `output/playwright/managed-pages-live.png` (local evidence, not committed).
Maintenance skill context updated to distinguish this picker scope from manual/Excel
backend policy.

Backup/rollback point:
`/mnt/data-disk/fb-video-publish/backups/managed-page-picker-20261023T094046Z-39b44227/manifest.json`.

Exact code-only rollback command on CPU:

```bash
python3 /mnt/data-disk/fb-video-publish/releases/39b4422/scripts/deploy_fb_managed_page_picker.py --rollback /mnt/data-disk/fb-video-publish/backups/managed-page-picker-20261023T094046Z-39b44227/manifest.json
```

Then reload the browser. No service restart or database restoration is needed.
