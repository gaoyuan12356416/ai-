# 实施计划与文件边界

1. 从已有 YouTube HK 分支建立 `codex/youtube-auto-publish-20260910` 独立 worktree；将当前生产 app/导航的小范围差异保留为 baseline，原本地脏目录不变。
2. 并行实现共享后台风格 UI、封面审核服务、 reviewed publisher；原上传流程维持独立 claim。
3. 增加受 Cookie+模块+导航保护的路由、私有封面资源、默认设置。
4. 配置仅注释的 SQL 文件；媒体与生图目录放在挂载的数据盘。
5. 离线单元/浏览器/权限测试；编译和 live feature guard；GitHub exact commit 后再部署精确文件。
6. 备份源文件/SQLite/服务配置；替换统一记录 writer、API 和旧 publisher，再启用新 worker；检查公开页面和登录空态。

验证命令：`python scripts/test_youtube_auto_engine.py`、`python scripts/test_youtube_auto_service.py`、HTTP 集成测试、现有 synthesis/YouTube 回归；`python -m py_compile` 新旧变更模块；`node --check static/youtube-publish.js`；`python scripts/verify_live_feature_guard.py --root .`。浏览器用 `tests/youtube_publish_browser_qa.js` 进行 mock API 行为验证。
