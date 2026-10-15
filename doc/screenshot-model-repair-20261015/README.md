# Screenshot generation model compatibility repair

The four CPU screenshot workers still selected `gpt-5.5`. On 2026-10-15 the
ChatGPT provider rejected that model before image generation. CLI startup
messages and the echoed prompt preceded the provider error in notifications.
An obsolete plain-file `CODEX_HOME/skills` sentinel also caused startup errors.

Use the operator-selected `gpt-6.1-sol`, preserve low reasoning in deployed
units, create a real isolated skills directory, report the terminal provider
error first, and stop per-size fallback for unsupported-model errors. Existing
source consistency, output ratio, image-generation, and cache rules are retained.
Health responses expose the selected model and reasoning effort.

The baseline commit records the actual deployed screenshot source (SHA256
`9d410bc9e33dccd765a5b0ead95c8277c3b1c3937305a40b88a9a4313118aa93`).
It intentionally preserves the deployed no-output retry and subprocess behavior
instead of restoring unrelated older branch behavior.

Validation: `python scripts/test_screenshot_model_compatibility.py`,
`python -m py_compile codex_screenshot_service.py`, and `git diff --check`.
Production acceptance requires a real three-ratio generation and decoded output
readback; a health endpoint is only a process/configuration check.

Deploy only the screenshot module and four screenshot units. Save source,
systemd configuration, UI compatibility copies, and an online SQLite backup on
the verified data disk before switching. Use an immutable GitHub release and
dedicated unit overrides. Preserve the previous source and unit overrides for
rollback, without restoring the database or reverting successful recovered jobs.
Freeze today's failed IDs using Asia/Shanghai boundaries converted to database
UTC, then use the existing retry API; never directly reset database rows.

Official configuration: https://learn.chatgpt.com/docs/config-file/config-reference
Model migration: https://developers.openai.com/api/docs/guides/latest-model

## Deployed configuration and rollback

CPU host: `43.166.187.96`. Code release: `0ba7eefa1033adc20e3130b07693713facff445a`.
All four screenshot workers returned `gpt-6.1-sol` / `low` after the scoped
restart; the main API retained PID `1171997`. Runtime source is
`/mnt/data-disk/codex-workers/us-migrated/releases/0ba7eefa1033adc20e3130b07693713facff445a/runtime/codex_screenshot_service.py`;
the maintainable main-runtime copy is `/root/drama_material_service/codex_screenshot_service.py`.
Unit overrides are named `95-screenshot-model-repair.conf`.

Backup and frozen recovery state:
`/mnt/data-disk/codex/tasks/screenshot-model-repair-20261015/before-deploy`.
The 2026-10-15 Beijing natural day corresponds to database UTC
`[2026-10-14 16:00:00, 2026-10-15 16:00:00)`. Both created-day and
finished-day checks found the same 26 failed jobs; all 26 had unsupported-model
errors, no successful sizes, and received one HTTP 202 retry receipt.
Receipts and original errors remain in the private task directory, outside Git.

To inspect without changing jobs:

```sh
python3 /mnt/data-disk/codex/workspaces/screenshot-model-repair-20261015/ops/screenshot-model-repair-20261015/recover.py status
```

Rollback after draining screenshot work:

```sh
python3 /mnt/data-disk/codex/workspaces/screenshot-model-repair-20261015/ops/screenshot-model-repair-20261015/rollback.py
```

Rollback restores source and removes only this rollout's four dedicated
unit overrides, then restarts those four workers. It never restores the old
SQLite snapshot or removes new images. The former model is rejected upstream,
so rollback is an emergency code/configuration restore, not a working model fix.

## Verification and background handoff

The operator requested that Codex stop continuous monitoring and leave the
batch to the existing backend workers. No new monitor or automation was created.
At the last checked snapshot, all 26 original failures had received one accepted
retry: 4 jobs were done, 21 remained failed due to upstream content rejection,
and 1 was still processing. These are handoff counts, not final batch counts.

Seventeen retained public images passed HTTP GET, strict decoding, exact output
dimensions and SHA256 recording. A Windows-side public GET independently matched
the server hash. One complete three-ratio set and one partial portrait were
visually inspected against their original covers. The original screenshot job
remained blocked by image-generation content moderation in this retry.

Five targeted regression tests passed both locally and on CPU, as did compilation
and diff checks. Four previously successful jobs retained their images and
completion timestamps; SQLite quick_check returned ok. The main API PID stayed
unchanged. Skill context now requires background handoff for long generation
batches unless the operator explicitly requests continued monitoring.

The remaining job continues in the backend independently of the chat. Inspect
later through the existing task UI or existing notification mechanism; do not
replay the frozen receipt set or create chat polling automations implicitly.
The private operation directory retains frozen failures, all 26 accepted retry
receipts, the last status snapshot, and image verification receipts.
The compatible backup entry is
`/root/drama_material_service/backups/screenshot-model-repair-20261015`, pointing
to the data-disk backup above.
