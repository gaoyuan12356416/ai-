# Feature Modules

The AI backend is a shared deployment, but business projects must live in separate feature directories.

Target module layout:

- `features/drama_synthesis/`: 剧集合成
- `features/cover_synthesis/`: 封面图合成
- `features/ad_material_tasks/`: 投放素材任务
- `features/voiceover_drama_tasks/`: 配音剧语种任务
- `features/x_accounts/`: X OAuth 账号授权与 Token 隔离 sidecar
- `features/x_posts/`: X 单条灰度发布、W2A 短链及发布日志

`app.py` should only keep common infrastructure and route dispatch. New business logic must not be added directly to `app.py`.

Frontend pages should also be split by feature:

- `static/drama-synthesis.html`
- `static/screenshots.html`
- `static/ad-material-tasks.html`
- `static/voiceover-drama.html`

During migration, the live-feature guard in `deploy/live_feature_guard.json` prevents deploying a branch that removes an already-live module.
