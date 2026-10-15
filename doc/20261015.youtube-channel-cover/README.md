# YouTube channel cover requirements (2026-10-15)

Channel editors can save reusable AI cover requirements beside the title, description and first comment. Selecting a channel fills its nonblank requirements into the new-publication form. Publishers can edit them for one publication. Blank or whitespace-only defaults preserve the current form value, including when switching channels.

## Interface and data

- Existing `GET/POST /api/youtube-auto-publish/channels/{local_id}/template` adds `cover_requirements_template`, a string defaulting to `''`. POST accepts empty text or up to 2000 characters after trimming. Text is literal; there is no macro expansion for cover requirements. Non-string values fail validation.
- An omitted cover field from an older cached editor preserves the existing value in the same version-CAS transaction. An explicit empty string clears it. The three original copy fields remain required in template POST requests.
- `configured_fields` on `/channel-list` includes `cover_requirements` when set. Tenant-plus-real-channel identity, Cookie/module/navigation permissions, editor metadata and the ability to configure blocked channels are unchanged.
- Add `cover_requirements_template TEXT NOT NULL DEFAULT ''` to `youtube_channel_template`. Initialization checks the schema inside `BEGIN IMMEDIATE`; fresh creation, repeated startup and concurrent initialization are safe. Existing template values and versions are preserved.
- The browser maps the default into `draft.requirements`. Only channel selection or explicit template-read retry applies it. Authorization refresh, later template saves and background workers do not reapply defaults. Loading locks the affected fields and submission; cancellation/selection guards reject stale responses. A missing cover field in an older GET response is treated as blank.
- `POST /tasks` still submits `requirements`; this final text remains frozen under the original operation ID, including retries, later template edits, generation and review. Local cover uploads do not invoke AI generation.

## Changed deployment surface

Only `features/youtube_auto_publish/channel_templates.py` and the five channel/publish static assets are installed. Both `/root/drama_material_service/static` and `/usr/share/nginx/html` are updated. HTML cache versions are `20261015-cover-requirements-v1`. The template editor scrolls its input area while keeping the save controls visible on desktop and mobile.

The branch starts from `codex/youtube-manual-short-links-20260923` (`7ce6b6c`). The six affected files were compared against live CPU bytes before editing. The scoped installer checks their exact pre-deploy hashes; it does not deploy the branch's older `app.py`, workflow service, generation runtime, worker runtime or worker entrypoint. In particular, retain the 2026-10-12 bounded independent cover-preparation dispatch and newer production authorization/runtime fixes.

## Validation

Local validation passed:

- 12 channel-template tests: literal save/read/clear, 2000-character boundary, invalid types, tenant isolation, shared editors, concurrent CAS, legacy-client omission, repeated/concurrent old-schema migration and final submitted requirements frozen through generation.
- Existing HTTP 24, preparation service 45 and channel-directory 19 tests.
- Existing template deployment tests 3 and new scoped deployment tests 3: migration preserves unrelated facts, source backup is unchanged, rollback retains the current database, rejects later drift and starts the API even if stop reports an error.
- 41 mocked browser assertions covering save/reopen/clear/conflict, configuration badge, automatic application, empty defaults, manual edits, channel refresh/switching, slow/error/stale replies, old GET compatibility, local/AI source switching, final submitted requirements and desktop/mobile layout. Mocked 409/503 responses are deliberate failure-path checks; no production task, generation, notification, upload or comment is created.
- Required Python compilation, changed JavaScript syntax and `git diff --check`.

Screenshots: `output/playwright/youtube-channel-template-desktop.png`, `youtube-channel-template-mobile.png`, `youtube-template-publish.png`.

## Deploy and rollback

After pushing the exact commit, fetch it into a clean CPU release checkout under `/mnt/data-disk/deploy/youtube-auto-publish/releases/<commit>`. Run on CPU:

```bash
python3 <release>/scripts/deploy_youtube_channel_cover.py --check
python3 <release>/scripts/deploy_youtube_channel_cover.py
```

The installer verifies the mounted data-disk UUID, available space and all live target hashes; rehearses migration twice on an online SQLite backup; saves file backups, a second online DB backup and a manifest; then stops only `drama-material-api.service`, migrates, installs exact GitHub bytes and starts it in `finally`. File-install failure restores old files. Failed API health triggers code rollback. Main API may briefly be unavailable; publisher/worker units are not stopped or restarted. Worker PIDs/states and the protected runtime source hashes must remain unchanged.

For the exact backup directory printed by the installer:

```bash
python3 <release>/scripts/deploy_youtube_channel_cover.py --rollback <backup>
```

Rollback validates paths, old/new file hashes and refuses later file drift. It restores only these code/static files and restarts only the API. Keep the additive column, current SQLite, saved defaults, images, tokens, outbox and every publishing fact. Never restore `before.sqlite3` over the live database.

After deployment, verify the public versioned assets against release hashes, unauthenticated API 401, authenticated template GET including the new string, service health, protected runtime/worker fingerprints and unchanged historical template fields. Record operational evidence in `deployment-evidence.md` without credentials or account-specific data.
