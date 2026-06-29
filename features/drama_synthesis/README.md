# 剧集合成

Target owner for drama synthesis backend and frontend code.

Current migration status: route and page boundary migrated.

- Backend API route dispatch: `features/drama_synthesis/routes.py`
- Standalone frontend page: `static/drama-synthesis.html`
- Core video job orchestration still lives in `app.py` during this migration batch. Move deeper service logic here in smaller follow-up changes instead of adding new drama-synthesis business logic to `app.py`.
