# Result preview selection fix

## Scope and review
- Both index.html and drama-synthesis.html use the persisted job.outputs booleans for compilation, no-BGM, random-template and cover preview visibility.
- Unselected outputs never display a card, including stale/internal URLs. Selected outputs without a URL retain the existing pending placeholder.
- Detail output cards follow the same rule. Screenshot-material jobs, downloads, short links, YouTube status and generation behavior are unchanged.
- Missing outputs fail closed. The API already returns normalized booleans; no backend or schema change is needed.

## Tests
- node scripts/test_drama_synthesis_list_actions.js: 86 checks, both pages, all 16 selections with pending and ready URLs, absent output settings and existing list actions.
- node scripts/test_drama_youtube_modal_loading.js: 38 checks and inline-script syntax validation.
- git diff --check: pass.

## Deployment and rollback
Deploy only static/index.html and static/drama-synthesis.html from the pushed GitHub commit to /root/drama_material_service/static and /usr/share/nginx/html on 43.166.187.96. Compare the live baseline with parent commit ignoring line endings before replacement; abort on drift. Back up all four files under /mnt/data-disk/drama-preview-selected/backups/20260828. No service restart required.

Rollback: copy each backup file from runtime/ to /root/drama_material_service/static/ and public/ to /usr/share/nginx/html/, then refresh the page. Do not restart workers or reset tasks.

Automated verification does not substitute for user visual acceptance. Skill context unchanged: this narrow presentation fix is documented here and does not change generation contracts.
