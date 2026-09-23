# Deployment and rollback

Host 43.166.187.96. Runtime /root/drama_material_service; public static /usr/share/nginx/html. GitHub-first exact release; source branch codex/youtube-manual-short-links-20260923 based on current channel-template frontend. Preserve later independent production modules.

Run scripts/deploy_youtube_manual_links.py --check from the clean exact-commit server checkout. Verify data-disk UUID, nine target files and live app hash; then run without --check. It backs up all changed files and SQLite online, stops only drama-material-api.service, atomically installs files, starts API and checks health. Two existing files are changed (app route and page HTML); four new files are added, with three static copies also installed publicly. No worker restart or Nginx reload required.

Rollback: python3 RELEASE/scripts/deploy_youtube_manual_links.py --rollback BACKUP. Uses manifest-installed hashes, rejects later drift, restores only code/static, retains all SQLite tables and short-link files. Fill exact release/backup in deployment-evidence.md after execution.
