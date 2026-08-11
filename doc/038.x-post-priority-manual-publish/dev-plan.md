# 开发计划

## 开发范围

在生产 X 基线 `29bd900` 的 GitHub 文档后继 `3998ee4` 上实现短剧高优和素材手动异步发布，不修改其他后台模块。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 需求、SA、API、部署与 QA 文档 | Codex | `doc/038.x-post-priority-manual-publish/` | 已完成 |
| additive schema、高优及手动批次状态机 | Codex | `features/x_posts/service.py` | 已完成 |
| 明确素材 ID selector | Codex | `features/x_posts/selector.py` | 已完成 |
| Sidecar 与主后台 API/client | Codex | `features/x_accounts/`, `app.py` | 已完成 |
| 手动 runner 与 systemd 单元 | Codex | `scripts/x_post_manual_runner.py`, `deploy/` | 已完成 |
| 页面交互及发布日志批次标识 | Codex | `static/x-post-*.html` | 已完成 |
| 单元/接口/DOM/迁移回归 | Codex | `scripts/test_x_post_*.py` | 本地通过 |
| GitHub-first 生产备份、部署和无发帖验收 | Codex | CPU 43.166.187.96 | 已完成 |

## 编译 / 构建命令

```powershell
python -m py_compile app.py features\x_posts\service.py features\x_posts\selector.py features\x_accounts\client.py features\x_accounts\oauth_service.py scripts\x_post_manual_runner.py
python -m unittest discover -s scripts -p "test_x*.py"
node -e '<extract and parse all inline scripts in the three changed X pages>'
python scripts\audit-publish-ledger.py --db <sqlite-copy> --json
git diff --check
```

## 风险与依赖

- 依赖当前 production Sidecar、只读素材 MySQL、GPU repair tunnel 和 X token sidecar。
- 生产 schema 只能 additive migration；部署前必须在 SQLite 在线备份副本演练。
- 手动 runner 必须与自动排期共享锁，禁止部署验收触发真实发布。
- 主检出有用户未提交改动，开发只在独立 worktree 进行。

## 完成记录

- 2026-08-11：完成生产 Git blob 对齐并建立独立分支/需求目录。
- 2026-08-11：完成高优、手动 durable run/queue、runner/timer、管理 API/UI 和发布日志标识。
- 2026-08-11：完成本地 X 全回归并修正 Linux 测试夹具路径隔离。
- 2026-08-11：精确提交 `5f9084b59bb14d1efd806ed32d070a6b2ee851c1` 已部署；数据库副本双迁移、自然 timer 空跑及 150/150/149 零增量验收通过。
