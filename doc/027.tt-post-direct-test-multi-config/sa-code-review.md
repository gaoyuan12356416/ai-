# SA 代码评审门槛

## 当前结论

027 基线已有历史部署与回归证据。BUG-005 已完成 diff 复核、T01-T12 自动化覆盖、全量回归、服务器候选测试和生产只读验收。慢 `/tasks` 响应覆盖新筛选的竞态已增加 request generation 保护，并把下一轮轮询延后到 direct/tasks 两个 GET 均完成后，UI 33/33 复验通过；当前已上线并关闭。

最终保留三个非阻塞优化项：页码分页只保证数据不变时顺序稳定，不承诺跨并发写入的历史快照；统一 DTO 后续可继续瘦身，当前生产 5 条响应仅 16,358 bytes，低于 App 1 MiB 上限；`processing_download` 是兼容筛选别名，页面已统一显示为“发布处理中”。三项均不影响本次已有任务漏显修复。

## P0 核对项

| 编号 | 核对项 | 通过条件 |
| --- | --- | --- |
| CR-01 | Schema | 只新增 auto-config/direct-test 两表及实际索引；无 direct-test event/auto-due 新表，既有 event 不变 |
| CR-02 | Auto config | GET/POST 顶层 `item`；POST 使用 `publish_times/source_account_ids/consent`；关闭不解析 accepted=false，新增成员仍校验可信快照/本地设置 |
| CR-03 | 立即测试路由 | POST `/test-publish`、GET `/direct-tests`；无管理端 detail/reconcile |
| CR-04 | 测试账号边界 | 显式单选，可不在 `account_ids`；版本只冻结模板；设置/creator/gate/consent 仍校验 |
| CR-04A | 客户端幂等 | submitted/unknown 非终态保留 key+version+consent；终态后的显式再测才换 key |
| CR-05 | 素材边界 | historical published 可测试；同素材 active/unknown direct/legacy 阻断；direct 不改 pool，明确终态不阻断后续 auto claim |
| CR-06 | 入池边界 | 一次一个素材；`source_account_id` 必须属于配置；模板取保存版本 |
| CR-07 | 发布投影 | 只有 published/unknown/unpublished，扁平字段和计数与 core 聚合一致 |
| CR-08 | 同分钟时序 | 所有当前 due slots 的 claim 尝试早于旧 recovery/新 run 的首个 creator-info；每个 claim 独立原子；limit 不截断 preclaim |
| CR-09 | 恢复与未知 | claimed/unbound 用现有 run/pool 恢复；unknown 不自动重发；内部 reconciliation |
| CR-10 | 兼容接口 | `/run-now` 保留；页面立即测试不调用它 |
| CR-11 | 安全 | token/secret/claim token 不进入 API/UI 日志；生产验收无写请求 |
| CR-12 | 统一任务读模型 | 新 GET `/tasks` 在服务端合并 queue/direct-test 后筛选、稳定排序、分页前统计再分页；无前端双分页拼接 |
| CR-13 | 类型与 ID | item 有 `task_key/task_type/task_id`，源 ID 互斥；相同数字 ID 不串任务 |
| CR-14 | 操作隔离 | queue events/cancel/reconcile 保持；direct 三项能力关闭，UI 不生成 queue action；不新增 direct event/管理写路由 |
| CR-15 | `/queue` 兼容 | 旧 `/queue` 的 items/summary/pagination/排序、错误合同和旧自动任务行为逐字段回归 |
| CR-16 | 只读安全 | `/tasks`、筛选和翻页不写 DB、不唤醒 runner、不调用 GPU/COS/TikTok、不创建 Post |

## 文件级核对

- `features/tt_posts/core.py`：schema、version-0 投影、原子保存、direct-test 状态机、发布聚合、现有 claim。
- `features/tt_posts/service.py`：精确字段集合、非成员测试账号、关闭 consent、material-level 阻断、同分钟两阶段循环，以及 BUG-005 统一任务只读投影。
- `scripts/tt_post_prepare_runner.py`、`scripts/tt_post_runner.py`：任务领取、恢复、内部核对，无真实测试旁路。
- `app.py`：允许路由与 service 一致；`/tasks` 仅 GET、查询参数白名单、权限和脱敏保持。
- `static/tt-post-pool.html` 及部署副本：多选成员、独立测试账号、单素材入池、三态展示、dirty/version 控制，以及任务类型标记/筛选、direct 只读操作门禁。
- `scripts/test_tt_posts_service.py`、`scripts/test_tt_post_pool_ui.py`、`scripts/test_tt_posts_app_contract.py`：T01-T12 的服务、UI 和代理合同；如查询下沉到 core，补 `scripts/test_tt_posts_core.py`。

## 评审输出要求

- 精确 commit 与工作树 diff；
- 9 个测试脚本逐项退出码/通过数；
- schema 二次初始化与 `PRAGMA integrity_check`；
- 同分钟首个网络调用前的 DB/调用顺序证据；
- 生产只读 0 副作用证据与回滚点。
- `/queue` 增量前后快照、混合列表跨页 task-key 集合、同号 ID 操作隔离证据；
- BUG-005 targeted 与 9 个脚本全量结果为 `341/341`，Node bridge 为 `53/53`；不得以旧基线 `334/334` 替代。
