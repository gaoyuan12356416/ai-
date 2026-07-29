# 开发计划

## 开发范围

短剧池选择、账号归属存储、计划事务、发布前防线、sidecar 契约、页面展示和生产迁移。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 现网历史与定时任务只读审计 | Codex | 生产 SQLite/systemd | 完成 |
| 数据库字段、迁移、索引与触发器 | Codex | `features/x_posts/service.py` | 完成 |
| sidecar 与 selector 账号映射 | Codex | `oauth_service.py`、`drama_selector.py`、runner | 完成 |
| 冻结旧队列发布前阻断 | Codex | `service.py` | 完成 |
| 绑定账号页面展示 | Codex | `x-post-drama-pool.html` | 完成 |
| 未绑定坏剧 FIFO 顺延与 fail-closed 边界 | Codex | `service.py`、`oauth_service.py`、schedule runner | 完成 |
| 10:06 失败批次与坏剧遗留状态只读审计 | Codex | 生产 SQLite/systemd journal | 完成 |
| 单元与集成回归 | Codex | `scripts/test_x_post_*.py` | 完成 |
| GitHub 推送与生产部署 | Codex | 不可变 release | 完成 |
| 补足第5部合规新剧 | 管理员 | Post短剧池 | 待管理员提供/添加短剧ID |

## 编译 / 构建命令

```text
python -m py_compile features/x_posts/service.py features/x_posts/drama_selector.py features/x_accounts/oauth_service.py scripts/x_post_schedule_runner.py
```

## 风险与依赖

- 依赖当前 SQLite 结构与既有队列/日志证据。
- 升级必须避开短剧定时点并确认没有非终态批次。
- 根盘空间紧张，备份、演练和 release 必须放数据盘。

## 完成记录

- 2026-07-28：本地实现完成，327 个相关测试通过；等待生产演练。
- 2026-07-29：确认 10:06 批次因未绑定坏剧 `3CRScaBEY0` Episode 1 时长不合规而提前结束；完成 FIFO 顺延、绑定/历史剧 fail-closed、失败批次不可重建和内部校验入口，335 个完整 X 回归测试通过。
- 2026-07-29：生产发布 `569640e8ab737aaf720d2cfc1e7c7978a14d24dd`；池53、54经真实媒体证据校正为 `validation_failed`，10:06原批次保持0队列/0日志，timer恢复。当前仍差1部合规新剧覆盖账号5。
