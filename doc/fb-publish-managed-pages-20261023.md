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
