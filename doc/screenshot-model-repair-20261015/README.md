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
