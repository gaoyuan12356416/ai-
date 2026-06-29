# 投放素材任务

Target owner for ad material task backend and frontend code.

Current migration status: route and page boundary migrated.

- Backend API route dispatch: `features/ad_material_tasks/routes.py`
- Standalone frontend page: `static/ad-material-tasks.html`
- Core task state-machine functions still live in `app.py` during the first migration batch. Move deeper service logic here in smaller follow-up changes instead of adding new ad-material business logic to `app.py`.
