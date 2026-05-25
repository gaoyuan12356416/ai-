# 封面图合成

Target owner for cover/screenshot synthesis backend and frontend code.

Current migration status: route boundary migrated.

- Backend API route dispatch: `features/cover_synthesis/routes.py`
- Standalone frontend page: `static/screenshots.html`
- Screenshot job orchestration and generation helpers still live in `app.py` and sidecar files during this migration batch. Move deeper service logic here in smaller follow-up changes instead of adding new cover-synthesis business logic to `app.py`.
