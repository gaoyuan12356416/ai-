# 配音剧语种任务

Owner for voiceover drama designer task backend logic.

Current migration status: active.

- Backend service: `features/voiceover_drama_tasks/service.py`
- Frontend page: `static/voiceover-drama.html`
- Shared `app.py` keeps only auth checks, route dispatch, audit logging, and dependency injection for this module.
