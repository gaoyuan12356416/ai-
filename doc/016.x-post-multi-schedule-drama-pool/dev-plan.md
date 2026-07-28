# 开发计划

## 开发范围

在既有 X Post SQLite/sidecar/AI 后台基础上，以增量迁移方式增加多账号多时间排期和短剧池，不重建现有发布历史。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 需求、架构和测试设计 | Codex | `doc/016.*` | 已完成 |
| 排期配置与冻结批次 | Codex | `features/x_posts/service.py` | 已完成 |
| 短剧源表审计与顺序选择 | Codex | `features/x_posts/drama_selector.py` | 已完成 |
| claim/worker 运行器 | Codex | `scripts/x_post_schedule_*.py` | 已完成 |
| sidecar 与后台 API | Codex | `oauth_service.py`、`client.py`、`app.py` | 已完成 |
| 素材池/短剧池页面 | Codex | `static/*.html`、导航 | 已完成 |
| 短剧池当前页全选与原子批量删除 | Codex | `service.py`、sidecar/client、`app.py`、短剧池页面 | 已完成 |
| 单元、合同、UI 回归 | Codex | `scripts/test_x*.py` | 已完成 |
| GitHub 推送、生产部署与浏览器验收 | Codex | CPU 服务器 / AI 后台 | 待执行 |

## 编译 / 构建命令

```bash
python -m py_compile app.py features/x_accounts/client.py features/x_accounts/oauth_service.py features/x_posts/service.py features/x_posts/drama_selector.py scripts/x_post_schedule_claim_runner.py scripts/x_post_schedule_runner.py
python -m unittest discover -s scripts -p "test_x*.py"
git diff --check
```

## 风险与依赖

- 依赖 CPU 服务器现有 X token、只读 MySQL、数据盘、GPU 修复 token 和 COS 流程。
- 新旧 timer 不可并存；必须先停用并 mask `x-post-daily.timer`。
- 线上 `navigation.json` 有人工权限配置，部署必须按 key 合并，不能覆盖整份文件。
- SQLite 发布历史是审计事实；回滚代码时不得回滚已发生发布后的数据库。

## 完成记录

- 2026-07-27：核心实现、边界修复和聚焦测试完成。
- 2026-07-28：完成短剧池可删除状态下沉、当前页全选、原子批删接口、审计及回归用例。
