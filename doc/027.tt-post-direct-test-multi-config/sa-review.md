# SA 需求与方案评审

## 结论

**方案通过，完整回归与只读生产验收完成前不允许部署。**

## 锁定方案

1. 立即测试使用独立 `tt_post_direct_test`，入口为 `POST /api/admin/tt-posts/test-publish`；不从自动池选材，不写 legacy queue/run/pool。
2. 目标账号是独立显式单选，可不属于 auto-config 账号集合；`expected_config_version` 仅锁定已保存描述模板。账号仍需所有人可见、允许评论、实时 creator-info 兼容、门禁开放和本次 consent。
3. 历史 published 素材允许新测试；同素材活动/unknown direct-test 或 legacy queue 阻断新建。auto/direct 互斥也只覆盖同素材 active/unknown；direct `published|failed|canceled` 后自动池仍可正常消费。发布 claim 保留账号串行。
4. 自动入池仍是一素材一请求，`source_account_id` 必须属于已保存 auto-config；多选成员不能成为隐式“第一个账号”。
5. 自动配置以 `tt_post_auto_publish_config(id=1)` 保存描述、开关/时间、成员和 consent，一个 version 原子更新；关闭不要求新 consent/creator-info。关闭态保留/移除旧成员无需远端，新增成员仍需可信账号快照与本地设置。
6. 账号状态字段锁定为 `auto_publish_selected`、`auto_publish_state(active|paused|attention_required|not_selected)`、`auto_publish_config_version`。
7. 发布投影直接合并到素材 item，状态只有 `published|unknown|unpublished`；`consumed` 和活动任务都不推断为 published。
8. 同分钟不新增 due 表。门禁开放时，先对全部当前 due slots 调现有 `claim_recurring_run`；每个调用独立 SQLite 原子预占 run+精确 FIFO 素材。所有尝试结束后才处理旧 claimed/unbound recovery 或新 run，任何 creator-info 都不能抢在 preclaim 前；`limit` 只限执行。
9. 数据层只新增 `tt_post_auto_publish_config` 与 `tt_post_direct_test`；不新增 direct-test event、auto-due 或 direct-test account index，既有 `tt_post_event` 不变。
10. 旧 `/run-now` 兼容接口保留，但新 UI 不调用。

## 关键失败语义

- 同键同事实返回原 direct-test；同键异事实 409。
- 无保存配置版本、版本冲突、账号设置不满足、门禁关闭或同素材活动/unknown：创建 0 写入、0 GPU 调用。
- auto-config mixed legacy 首次保存必须明确统一时间并保持关闭；第二次才允许启用。
- 单个 due slot 无素材时该 claim 失败且不建空 run；其余 slots 继续预占。实现不承诺整批回滚。
- publish 不确定进入 `unknown`，由内部 reconciliation 处理，管理端没有人工 resolution API。

## SA 验收门槛

- schema、路由、字段、状态枚举与 `core.py/service.py/app.py` 一致；
- 真实存在的 9 个 TT 测试脚本全部通过；
- 测试证明首个 creator-info 前已经尝试预占全部同分钟 slots；
- 生产验收只读、无真实 Post、无配置保存；
- 回滚只切 release，不覆盖 SQLite/ledger/manifest/COS。

## 评审记录

- 2026-08-03：初审。
- 2026-08-03：按实现复核，删除不存在的 event/due 表设计，纠正 `/test-publish`、账号独立合同、三态发布字段及 existing-claim 方案。
