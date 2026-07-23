# 012.x-post-material-pool SA 代码评审

## 结论

代码评审 GO。审查时生产部署待门禁；门禁已于 2026-07-23 关闭并完成精确 commit `75f46e7` 的生产部署，生产证据见 `deploy.md` 与 `test-report.md`。人工池、FIFO、Dramawave 门禁、三条成组、成功态联动、双向永久占用、派生统计、两级扫描窗口和检查结果分批均已闭环，最终离线回归 139/139 通过。

## 评审范围

- `features/x_posts/service.py`
- `features/x_posts/selector.py`
- `scripts/x_post_daily_runner.py`
- `features/x_accounts/oauth_service.py`
- `features/x_accounts/client.py`
- `app.py`
- `static/x-post-material-pool.html`
- 导航、环境示例和相关 `scripts/test_x_*.py`

## 问题清单

| 编号 | 严重级别 | 问题 | 影响 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-001 | P1 | 初版只在入池时检查历史 queue，pool-first 后非池 enqueue 可绕过关联 | 已占用素材可能仍显示可删/可用，破坏永久排重 | queue 写入、池插入/删除触发器、query/delete/check 全部按 pool ID 或 material key 防护 | 已关闭并回归 |
| CR-002 | P1 | manual `_pool_material_rows` 未显式读取并校验 `ads_custom_source.product` | 其他产品素材可被拼成 Dramawave W2A 发布 | SQL 与行级均精确校验 `product = 'Dramawave'` 并补负例 | 已关闭并回归 |
| CR-003 | P2 | `query_pool` 的逐项 CASE 把检查错误派生为 validation_failed，但 summary.available 仅按未发布且无 queue | API summary 与筛选结果不一致 | summary 显式排除 `last_error_code<>''` 并补断言 | 已关闭并回归 |
| CR-004 | P2 | 候选上限 50 曾同时作为原始池读取上限 | 前 50 条不合规会遮挡后续素材 | runner 按 scan limit 读取最老 1000，再按 FIFO保留最多 50 条合规候选 | 已关闭并回归 |
| CR-005 | P3 | `material_keys_path` 和 legacy spend selector 仍保留但正式路径不再使用 | 配置和维护认知成本 | 后续清理或加 legacy 注释；不阻塞本需求 | 接受为技术债 |
| CR-006 | P2 | runner 可能把超过 100 条拒绝一次发给只接受 100 条的 check API，且 best effort 会吞掉 400 | 校验失败展示缺失、同一素材反复扫描 | 规范化后每 100 条调用一次，并补 205 条 100/100/5 回归 | 已关闭并回归 |

## 正向确认

- `x_post_material_pool.status` 有数据库 CHECK，只允许 `unpublished/published`。
- add、daily plan 和 published 联动使用显式事务；计划仍要求恰好三条和五项合规计数全为 0。
- available 与 plan 均检查 pool ID/material key，计划额外冻结 `pool_created_at` 并校验严格正序。
- known failure、unknown、`post_creating` 不调用 `_mark_pool_published`，且 queue 历史阻止再次选择。
- daily bearer 路由范围与 backend 管理路由分离；浏览器写接口有 Cookie admin、同源 JSON、审计和 no-store。
- 页面不使用 `innerHTML`，预览链接仅允许 `https://x.com/{user}/status/{id}`。
- runner 在建计划前完成账号、存储、selector、文案和媒体预检；不足三条不会创建部分 queue。

## 编译 / 验证结果

- `python -m py_compile ...`：通过（修正一次不存在文件名的命令后，实际目标全部通过）。
- `node --check static/quick-nav.js`：通过。
- 素材池/selector/daily/ledger/app contract：65/65 通过。
- X Post service/X accounts/owner backfill：74/74 通过。
- 最终合计：139/139 通过。
- CR-001 的临时 SQLite 反例已由服务层和触发器共同拒绝；CR-002/003/004/006 均有对应测试或 runner 合同断言。
- 本次代码评审阶段未连接生产 MySQL、未迁移生产 SQLite、未调用真实 X；这些部署门禁的后续结果见 `deploy.md`。

## 发布门禁

1. 已满足：CR-001/002 自动化负例、CR-003 summary 断言、CR-004 scan limit 合同、CR-006 check 分批。
2. 已满足：最终工作树完整 X 回归、`py_compile`、JS 检查和 `git diff --check`。
3. 待部署：生产只读 schema/product 抽样、SQLite 副本迁移、live composite 基线。
4. 待部署：GitHub 精确 commit/release、timer start_date 和三账号自然首轮验收。
