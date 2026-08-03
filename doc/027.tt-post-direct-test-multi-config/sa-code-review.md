# SA 代码评审门槛

## 当前结论

**待最终自动化回归和 diff 复核。** 当前文档按工作树实现合同记录，不等于已批准部署。

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

## 文件级核对

- `features/tt_posts/core.py`：schema、version-0 投影、原子保存、direct-test 状态机、发布聚合、现有 claim。
- `features/tt_posts/service.py`：精确字段集合、非成员测试账号、关闭 consent、material-level 阻断、同分钟两阶段循环。
- `scripts/tt_post_prepare_runner.py`、`scripts/tt_post_runner.py`：任务领取、恢复、内部核对，无真实测试旁路。
- `app.py`：允许路由与 service 一致。
- `static/tt-post-pool.html` 及部署副本：多选成员、独立测试账号、单素材入池、三态展示、dirty/version 控制。

## 评审输出要求

- 精确 commit 与工作树 diff；
- 9 个测试脚本逐项退出码/通过数；
- schema 二次初始化与 `PRAGMA integrity_check`；
- 同分钟首个网络调用前的 DB/调用顺序证据；
- 生产只读 0 副作用证据与回滚点。
