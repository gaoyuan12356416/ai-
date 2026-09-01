# 开发计划

## 开发范围

- 新短剧池定时队列的 pending/waiting/resolved 路由状态机。
- 发布日志前的媒体准备、账号能力校验和事务选 relay。
- schedule/log/episode DTO 与 UI 展示。
- 加法迁移、DB 围栏、测试、GitHub-first 部署与回滚。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 路由账本与事务 | Backend | `features/x_posts/service.py` | 已完成 |
| 媒体准备与复用 | Backend | `features/x_posts/publish_media_repair.py` | 已完成 |
| 发布入口与能力校验 | Backend | `features/x_accounts/oauth_service.py` | 已完成 |
| fixed/random 排期 | Scheduler | `scripts/x_post_schedule_runner.py` | 已完成 |
| UI 与 contract | Frontend/QA | `static/`、`scripts/test_x_post_*` | 已完成 |
| 部署与验收 | Ops | CPU server release/systemd/SQLite | 待执行 |

## 编译 / 构建命令

```bash
python -m compileall features scripts
python -m unittest scripts.test_x_post_schedule_runner scripts.test_x_posts scripts.test_x_post_multi_schedule_store scripts.test_x_post_premium_relay_repost
```

## 风险与依赖

- `x_post_queue.delivery_mode` 有历史 CHECK 和多表 FK，禁止 shadow rebuild；采用 companion additive schema。
- GPU 只复用现有协议；部署不得改变 GPU worker API。
- Main OAuth 是生产 composite 文件，部署只同步明确的共享文件，不整体覆盖。
- 自然平台发布证据只能在真实排期产生后获取，不能用测试帖子替代。

## 完成记录

- 2026-09-01：确认基线 `955e54b`，基线 259 项聚焦回归通过；生产 quick/FK 与空在途检查通过。
- 2026-09-01：完成需求/SA/测试/部署设计，开始并行实现。
- 2026-09-01：完成实现与独立审查；关闭 waiting 公平性、冻结剧集污染、媒体/relay DB 篡改、解析后崩溃恢复及 rollout writer 竞态。
- 2026-09-01：精确合并生产并行 commit `401069b`；关闭旧/非短剧日志误标与公共 DTO 内部元数据两项 P2；最终全量 X 回归 892 项无失败（890 通过、2 项按环境跳过），全仓仅保留 5 个已在干净基线复现的 TT 错误；未调用真实 X 写入。
