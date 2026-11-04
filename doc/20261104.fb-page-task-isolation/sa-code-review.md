# SA 代码评审

## 结论

原生与统一层代码评审通过，未发现剩余 P0/P1 实现阻断项。独立验证覆盖同素材防重、不同素材继续、跨 family 活跃互斥、晚回执、旧资源迁移、已知 ID 未确认任务的只读对账及异常准备隔离。发布和线上恢复结果仍由主任务记录；本文件不代替生产验收。

## 评审范围

- 原生：`fb_video_publish/{core,guards,pool_scheduler,worker}.py`、`fb_auto_posts/{core,publisher,hit_material,manual_batch,pool_extension}.py`、`tests/test_fb_page_task_isolation.py`。
- 统一层：`pipeline_dispatch/{core,adapters,service,fb_native,fb_isolation}.py` 最终代码与既有回归；专用 `scripts/deploy_fb_page_task_isolation.py` 的 overlay、drain、备份和失败恢复。
- 本评审未连接生产、未发起平台 POST、未变更任务数据；测试全部使用临时 SQLite 与 fake transport。

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-01 | P1 | `guards.py`、`pool_scheduler.py` | 历史未知记录没有合法 material_id 时不能精确隔离，也不能把任意当前池素材消费掉。 | `fb_video_page_unknown` 保守暂停且不消费；只有确定 Page+material 的未知才隔离当前条目。 | 已修复；独立核对 manual fallback、auto ledger fallback、池错误分类通过。 |
| CR-02 | P1 | `core.py`、`fb_native.py` late receipt | 旧未知发布收到成功回执时，新素材可能已经持有同 Page 写锁；不得被旧 broad index/trigger 拦住或释放后继 lease。 | running-only Page UNIQUE；跨族只在 NEW running 与已有 running 时互斥；已知 ID 精确恢复到 submitted。 | 已修复；Schema、bridge 同族/跨族后继 running、原生精确 attempt+lease 晚回执及错误身份反例通过。 |
| CR-03 | P2 | `fb_auto_posts/publisher.py:reconcile_next` | 新增 max_credentials 截断后，当前切片全部拒绝被误认为全部授权拒绝，转 unknown；未试的有效授权失去后续 GET 机会。 | 区分完整授权池与本次切片；尚有未试授权时保留 submitted 并轮换/退避。manual 同样核对。 | 已修复；完整授权池与截断切片已区分，未遍历完保持 submitted，独立新用例通过。 |
| CR-04 | P2 | `fb_auto_posts/core.py:create_run` | previous_run_backlog 不含 unknown/submitted，但租约已过期的 preparing/running 会让整个模板后续 slot 继续阻塞。 | 收敛已过期原生租约；保持正常 planned/ready 的原调度容量语义。 | 已修复过期 preparing/running 的状态收敛；活跃租约、正常 planned/ready 保留，独立测试通过。 |
| CR-05 | P2 | `scripts/test_pipeline_fb_source_locks.py` | 既有测试期望 canonical_task_not_claimable；新的 pending busy 分类应可安全重试，测试产生 2 个断言失败。 | 同步预期为 canonical_account_busy，保留 pending status 不变与无 attempt 断言。 | 已更新并独立重跑通过；pending busy 不改失败且不新增 attempt。 |
| CR-06 | P1 | `fb_auto_posts/core.py:create_run` | 删除 Page unknown precheck 后，malformed manual unknown 会使跨族 trigger 拒绝正常候选插入，可能回滚整个多 Page run。 | 每 Page 规划时仅对无法识别素材的历史保守跳过；正常 Page 继续，不恢复 broad unknown 拦截。 | 已修复；真实 create_run 两 Page 测试：非法未知 Page skipped、另一 Page planned，运行不回滚。 |
| CR-07 | P2 | `fb_auto_posts/core.py:reconcile_task` | 已知 ID 的 unknown 开始自动 GET 后，空 next_reconcile 会导致每轮扫描重查。 | 保留 unknown/material fence，并对 GET 施加既有退避。 | 已修复：auto 5 分钟，manual 60 秒，立即再次 claim 被拒用例通过。 |
| CR-08 | P2 | `deploy_fb_page_task_isolation.py:switch` | 停止服务后、symlink swap 前失败可能恢复 timer 却未恢复旧 service。 | 预建 symlink，记录 stop 请求，未 swap 时启动原 service；已 swap 时只作兼容前向修复。 | 已修复并静态复核：stop 请求标记在命令前，未 swap 原 service 恢复；9 个 timer 白名单及双 candidate 依赖齐全。 |
| CR-09 | P1 | `guards.py:install_backstops` | 历史跨族 malformed unknown 的 Page fallback 可能拒绝既有 known-ID 任务的 GET claim/回执落账。 | 仅身份不变、OLD running/submitted/unknown 到 NEW unknown 或 known-ID submitted 的 UPDATE 豁免；INSERT、改身份及 NEW running 仍校验。 | 已修复；双向 GET/receipt、running 到 unknown、禁止 INSERT/new running/改素材反例全通过。 |

## 并发与兼容性检查

- 同 Page 真正 running 仍由原生 UNIQUE、跨 family trigger、BEGIN IMMEDIATE guard 和中央 Page resource 共同串行化。
- unknown、unknown_outcome=1、孤立 auto ledger 的不确定性均保留同素材屏障，allow_repeat 不能越过；非法素材回退 Page 屏障。
- 新 schema 初始化不再先重建旧 submitted/unknown Page 唯一索引；迁移在事务内替换 index/trigger，支持新增合法多条未知/待回执后重启。
- 已知 ID 的 Graph GET 不产生新 POST；已提交回执可以与后继素材 running 共存。平台无法确认的任务不据超时自动重发。
- 手动未写入的过期 claim 只有 graph_id 为空、unknown_outcome=0、native attempt 与 central operation 都为空才归为发布前中断。
- 中央迁移精确删除本 job 的 Page fence，保留 material fence 与其他 job 的 Page lease；多个 legacy unknown 必须各自保留物料屏障。
- 15 条导入跳过记录由用户重传原 Excel；本变更不得自动生成/补发这 15 条。

## 编译 / 验证结果

1. 独立运行初版 `tests.test_fb_page_task_isolation`：12/12 通过（5.36 秒）；之后新增用例须随最终 diff 重跑。
2. 独立运行 `scripts.test_pipeline_fb_source_locks`、`test_pipeline_adapters`、`test_pipeline_native_imports`：36 项，34 通过，2 项仅旧错误码断言失败（CR-05）。
3. 独立运行 6 组统一层既有回归：`test_pipeline_dispatch_core`、`test_pipeline_coordinator`、`test_pipeline_claim_handoff`、`test_pipeline_prepare_recovery`、`test_pipeline_receipt_outbox`、`test_pipeline_legacy_convergence`，73/73 通过（20.96 秒）。
4. 独立临时反例 5/5 通过：真实 bridge 晚回执遇同 Page manual/auto 后继 running 均不改后继 lease；非法 auto ledger 素材 fallback；manual queued 和 auto ready 的 unknown_outcome=1 均不允许执行。
5. 最终独立运行原生隔离测试 23/23 通过（6.95 秒）；包括有界 GET 截断、known-ID unknown 对账及退避、多 Page malformed 规划、后发先到的 batch interval 保护及历史 malformed 屏障下的纯回执 UPDATE。
6. 最终独立运行统一层新增隔离测试 15/15 通过（6.61 秒）；多个同素材 legacy fence 分别释放、失效中心租约不获新凭证、后继 Page lease 保留、GET 异常隔离均通过。
7. 整合独立运行 native/dispatch isolation、source locks、adapters、native imports 的 71 项全通过（17.94 秒）；之后新增的原生用例已由第 5 项重跑覆盖。`git diff --check` 通过。最后触发器收尾后再次独立运行统一隔离 15 项 + source locks 5 项，20/20 通过（8.11 秒）。

专用发布脚本已检查：只覆盖已审 feature 白名单、逐文件核对运行版本、在线备份演练迁移、排空在途请求后切换、9 个常规 timer 原状态恢复，不恢复数据库，不重启历史一次性 timer。部分切换失败保持 FB 暂停并保留新 schema。生产现场的 mount UUID、BASE 文件一致性、drain 证明和健康检查须由实际 prepare/apply 命令确认。

准确产品边界：无写入中断归为明确失败并释放无关素材；不承诺旧失败通用自动重试。已发或结果不明的任务不自动重发。缺失/非法素材身份仍保守隔离所在 Page。15 条跳过 Excel 行不自动补发。
