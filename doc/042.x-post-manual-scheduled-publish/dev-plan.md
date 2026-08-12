# 开发计划

## 开发范围

在现有 durable manual run 上增加单次北京时间定时能力，覆盖 UI、主 API、Sidecar store、到期 claim、素材 reservation、测试、文档与生产发布。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 需求、SA、API、测试与部署文档 | Codex | `doc/042.x-post-manual-scheduled-publish/` | 已完成 |
| 时间字段、reservation schema/trigger、到期 claim | Codex | `features/x_posts/service.py` | 已完成 |
| 主 API 与 Sidecar 字段透传/DTO | Codex | `app.py`, `features/x_accounts/` | 已完成 |
| 手动弹窗立即/定时交互 | Codex | `static/x-post-material-pool.html` | 已完成 |
| 存储、API、UI、runner 回归 | Codex | `scripts/test_x_post_*.py` | 已完成 |
| GitHub-first 部署与无 Post 验收 | Codex | CPU production | 待执行 |

## 编译 / 构建命令

```powershell
python -m py_compile app.py features\x_posts\service.py features\x_accounts\client.py features\x_accounts\oauth_service.py scripts\x_post_manual_runner.py
python scripts\test_x_post_priority_manual_store.py
python scripts\test_x_post_manual_sidecar.py
python scripts\test_x_post_manual_runner.py
python scripts\test_x_post_multi_schedule_ui.py
python scripts\test_x_accounts_app_contract.py
python scripts\test_x_post_auto_template_bridge.py
git diff --check
```

## 风险与依赖

- 依赖当前生产 `x-post-manual.timer`、共享锁和 Sidecar internal bearer，不新增凭据。
- 生产 schema 迁移前必须在线备份 SQLite，并确认 active manual queue/run/unknown 均为 0。
- `service.py` 为双 runtime 模块，部署必须同时覆盖 immutable X release 和主 API copy。
- 公网页面由 `/usr/share/nginx/html` 提供，需同步应用 static 与 Nginx docroot。

## 完成记录

- 2026-08-12：15:03 重新读取生产基线；live release 已更新为 `46e0720b8eb6b3c7b29cb92830f3c74cec3dbe70`，Sidecar/main `service.py` 一致，queue/log `182/182`、published/Post `181/181`、active `0`、unknown `0`、SQLite integrity `ok`，manual timer natural `no_pending`。
- 2026-08-12：15:19:52 另一项已批准功能把 live release 更新为 `09d267db99da0736f45189dad218a7462e75aa1c`，新增 operator-manual pool/历史素材复用。旧基线部署脚本在首条 release gate 即退出，未停止 timer、未切服务、未改线上库。
- 2026-08-12：定时功能已重放到 `09d267db…`，冲突解析同时保留复用 UI/DB 语义；补充 active reservation 对既有 pool 自动候选的隔离和交叉回归。40 个 X 模块隔离执行共 627 项，625 通过、2 项按既有条件跳过、0 失败；Playwright 本地 mock 验证通过且未访问 X。生产备份副本迁移演练待最终 commit 推送后执行。
