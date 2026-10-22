# Production acceptance — 2026-10-22

Deployed at 10:30 Beijing time to `43.166.187.96:/root/drama_material_service`.
Runtime commit: `30a3afc1dae46d710b878d250fa26eb476115f94` on
`codex/drama-episode-range-20261022`. GitHub push, server fetch and exact detached
checkout were verified. Only the four intended app functions, the page hint
(runtime and public copies) and the new identity module changed on the server.

Eight regression tests passed on Windows (repository and live candidate) and
CPU (repository, live candidate and installed app). They run actual app
submit/upsert functions against disposable SQLite, including eight simultaneous
same-range requests that result in one accepted task. Syntax and whitespace
checks passed. Source metadata validation and worker dispatch are test doubles;
this was not an end-to-end video render or publication test.

An online production SQLite copy reproduced the reported case: existing 1–20
was blocked, 20–40 inserted independently, and all 43 historical rows stayed
byte-equivalent as serialized. No production task was created by validation.
After deployment, all 43 live drama task rows retained the same snapshot hash.

Deployment waited for active screenshots to complete. API PID changed from
2771300 to 3572348 and is active; the rendering worker (3608348), unified YouTube
writer (2937249) and auto-publisher (2576490) retained their PIDs and active states.
Public HTML returned HTTP 200 with the new hint and without the old hint. Both
HTML copies match. Installed identity-module bytes match the Git release.

Evidence is stored privately under
`/mnt/data-disk/deploy/drama-episode-range/validation/`:
`candidate-evidence.json`, `service-before.json`, `deployment.txt`, `acceptance.json`.
The backend maintenance skill project map was updated with the range identity.

## Rollback

Backup (source plus online database evidence):
`/mnt/data-disk/deploy/drama-episode-range/backups/channel-templates-20261022-103058-30a3afc1dae4`.
The directory prefix is inherited from the shared scoped installer.

```bash
python3 /mnt/data-disk/deploy/drama-episode-range/releases/30a3afc1dae46d710b878d250fa26eb476115f94/scripts/deploy_drama_episode_range.py --rollback /mnt/data-disk/deploy/drama-episode-range/backups/channel-templates-20261022-103058-30a3afc1dae4
```

Run when composition and screenshot tasks are idle; the script enforces that
check and rejects later file drift. It restores only code and restarts the main
API. Preserve current SQLite and all new tasks, recipes and outputs. Do not
restore the evidence database over live state.
