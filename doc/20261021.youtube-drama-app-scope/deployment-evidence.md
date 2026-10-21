# Production acceptance — 2026-10-21

Runtime commit: `1926812a04a57fb14489adb166288623bb93f6ac`.
Branch: `codex/youtube-drama-app-scope-20261021`.
GitHub push, CPU fetch and exact detached release checkout were verified before
installation. Deployment completed at 17:29 Beijing time.

Host and target: `43.166.187.96:/root/drama_material_service/features/youtube_auto_publish/drama.py`.
The one-file installer passed its live-SHA preflight and backed up source plus
online SQLite. No source database records were changed.

## Validation

- Windows and CPU Python each passed 85 targeted tests: drama association 28,
  auto service 45, reference workflow 9, scoped installer 3. Syntax and diff
  whitespace checks passed.
- Before installation, fresh production source lookup and authenticated public
  materials GET returned `ambiguous`, empty name/description and `link_ready=false`
  for the reported Thai material. The candidate resolver, with the same live
  reader and material SQL, returned `matched` and `link_ready=true`.
- After installation, fresh lookup and authenticated public materials GET both
  returned `matched`, nonempty name/description, empty conflict message,
  `link_ready=true` and `drama_cover_status=available`; HTTP 200. The post-deploy
  source lookup took 4.147 seconds (server observation, not end-user page time).
- Browser verification used the current public HTML/JS and the captured live
  authenticated material DTO, with authentication/list fixtures in an isolated
  browser and all API writes aborted. Selecting the material and entering the
  screenshot-style Thai title resolved `{name}`; description resolved `{desc}`.
  Macro warnings: 0; conflict messages: 0; browser console errors/warnings: 0.
  No task submission, generation, short-link creation or platform publication
  was performed by validation. This is preview acceptance, not a test upload.
- API PID changed `2136222 -> 2576027`, active/running, exit status 0.
  Auto-publisher drained gracefully `3432663 -> 2576490`, active/running, exit
  status 0. Unified writer stayed PID `2937249`; legacy publisher stayed inactive.
- From candidate validation immediately before deployment through authenticated
  post-deploy validation, the snapshots of 792 preparations, 792 publication
  ledger rows and 1233 short links had identical hashes. An earlier preflight
  read observed a background ledger status update before this stable baseline;
  it was retained, not reset. Preparation and short-link snapshots were stable
  throughout.

Private evidence is under
`/mnt/data-disk/deploy/youtube-auto-publish/validation/drama-app-scope-20261021/`:
`before.json`, `candidate.json`, `after.json`, `deployment.jsonl`,
`service-acceptance.json` and browser evidence. Local browser screenshots and
redacted evidence are in the task worktree's ignored `output/playwright/`.
The personal backend skill's YouTube reference was backed up and updated with
the product-scope contract; its pre-existing untracked status was preserved.

## Exact rollback

Backup:
`/mnt/data-disk/deploy/youtube-auto-publish/backups/channel-templates-20261021-172957-1926812a04a5`.
The `channel-templates` prefix comes from the shared backup installer; the
manifest contains only this resolver file.

```bash
python3 /mnt/data-disk/deploy/youtube-auto-publish/releases/1926812a04a57fb14489adb166288623bb93f6ac/scripts/deploy_youtube_drama_app_scope.py --rollback /mnt/data-disk/deploy/youtube-auto-publish/backups/channel-templates-20261021-172957-1926812a04a5
```

This rejects later file drift, restores only the previous resolver, restarts the
API and gracefully drains the auto-publisher. Confirm its new active PID before
declaring rollback complete. Preserve the current SQLite, all assets, short links
and publication facts; the SQLite backup is evidence, not a restore instruction.
Rollback restores the original cross-product lookup behavior.
