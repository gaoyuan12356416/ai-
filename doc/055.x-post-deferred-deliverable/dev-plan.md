# 开发计划

## 开发范围

从生产精确基线 `3b9cda698e8f4ad1a025a8f9e2e1dd6296c95769` 修复素材池未来
可投放时间状态机，补齐 UI/API/测试和错误目录。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 临时状态/候选复检/FIFO | Codex | `features/x_posts/service.py` | 已完成 |
| API 参数与审计统计 | Codex | `app.py` | 已完成 |
| 页面状态与提示 | Codex | `static/x-post-material-pool.html` | 已完成 |
| 单元/契约/UI 测试 | Codex | `scripts/test_x_post_*`, `scripts/test_x_accounts_app_contract.py` | 已完成 |
| 错误目录与部署证据 | Codex | `doc/055.x-post-deferred-deliverable/` | 已完成 |

## 编译 / 构建命令

```bash
python -m py_compile app.py features/x_posts/service.py features/x_posts/selector.py scripts/x_post_schedule_runner.py
python scripts/test_x_post_material_pool.py
python scripts/test_x_post_multi_schedule_store.py
python scripts/test_x_post_material_pool_selector.py
python scripts/test_x_accounts_app_contract.py
python scripts/test_x_post_error_catalog.py
node --check static/quick-nav.js
git diff --check
```

## 风险与依赖

- 依赖只读 MySQL `deploy_time` 权威值和自然素材排期 timer。
- 不合并基线之后尚未部署的媒体恢复改动，避免扩大上线范围。
- `service.py` 是双运行时文件，部署时必须同时更新 Sidecar release 与主 API 副本。

## 完成记录

- 2026-08-25：完成生产只读根因确认并建立独立 worktree/分支。
- 2026-08-25：完成 deferred 状态、到点源证据门禁、API/UI、专项测试和错误目录。
- 2026-08-25：完成 GitHub-first 双基线部署、在线备份、无真实发帖验收与自然 timer 观察。
