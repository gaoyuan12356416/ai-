# 测试报告

## 测试结论

离线功能/安全回归及最终独立静态复审均通过，未在部署阶段手工触发新的真实 Post。生产发布仍以服务器 Python 3.9、systemd 239 unit 校验、SQLite 副本迁移全部通过为条件。

## 测试范围

- 候选排序、四表违规、危险标签、剧映射、媒体门禁。
- daily run/queue/log 事务、全局素材排重、账号日排重、legacy canary。
- X upload/Create Post 成功、known failure、429、unknown、幂等重放。
- storage mount/原子写/空间、媒体 SHA-256 复核、ffprobe 最小环境。
- 管理员 API/页面、普通用户 gate、筛选查询和链接 allowlist。
- X OAuth、账号 owner/admin 隔离及旧功能回归。
- 独立 daily bearer、三账号/路由范围、loopback 禁代理、root-only env 启动。
- plan/publish outcome marker、短链 fsync durability、官方限流 problem type/code。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| daily selector/runner | 34 | 34 | 0 | 0 |
| ledger/migration | 9 | 9 | 0 | 0 |
| X Post service | 27 | 27 | 0 | 0 |
| X account sidecar | 43 | 43 | 0 | 0 |
| 主后台契约 | 8 | 8 | 0 | 0 |
| owner backfill | 4 | 4 | 0 | 0 |
| 合计 | 125 | 125 | 0 | 0 |

## 缺陷情况

滚动独立评审累计 32 个 P1 均已修复并有回归；最终独立结论 GO，残余 P0/P1 为 0。

## 验证证据

- Playwright：管理员日志页、run/log 表、账号+unknown 筛选查询参数、普通用户“仅管理员”gate 均通过；唯一 console error 为 mock server 未提供 favicon。
- Skill forward-test：首次发现审计脚本问题；修复后正反 fixture、状态枚举、只读 hash/mtime、quick_validate 全部通过，待最新 commit 独立复核。
- 生产只读 taxonomy 抽样：昨日 Dramawave 消耗前 1000 素材的 `resource_tags` 主要为 `high_quality`/AI 制作类；`source_tag_name` 实际多为剧名。明确中英色情/暴力词仍由 selector fail closed。
- 未使用真实 OAuth Token、未调用 X 发布、未生成第二条真实 Post。

## 遗留风险

- `TC-020` timer 下一次触发和 `TC-021` 三账号首轮必须在生产部署/自然触发后验收。
- 生产主后台为 composite；部署前 live `app.py` blob 必须仍与已审计基线一致。

## 发布建议

代码 GO：GitHub 精确 commit 推送后，生产备份/副本迁移/systemd 239 校验成功即可部署；`X_POST_DAILY_START_DATE=2026-07-24`，首轮由自然 timer 执行。
