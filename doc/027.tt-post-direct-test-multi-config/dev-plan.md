# 开发计划

## 实现边界

本需求修改 TT CPU 控制面、SQLite 状态、runner 和 TT 素材池页面。GPU 继续使用现有 prepare/publish 与 job-keyed ledger；每个 direct-test 生成新 `tttest-*` job ID。实现只新增 `tt_post_auto_publish_config`、`tt_post_direct_test` 两张表，不新增 direct-test event 或 auto-due 表；既有 `tt_post_event` 不变。

027 基线与 BUG-005 只读展示增量均已于 2026-08-03 部署。增量新增统一任务查询和主表展示，不改 schema、runner、GPU、发布状态机或旧 `/queue`；代码、专项自动化、全量回归、服务器候选测试和生产只读验收均已完成。

## 工作项与当前状态

| 工作项 | 真实落点 | 当前状态 |
| --- | --- | --- |
| 两张 additive 表与幂等初始化 | `features/tt_posts/core.py` | 已实现，待完整回归 |
| version-0 legacy 投影与 mixed 两步迁移 | `core.py` | 已实现，待完整回归 |
| 描述、开关/时间、账号集合单版本原子保存 | `core.py`、`service.py` | 已实现；关闭不要求新 consent，新增成员校验可信快照/本地设置 |
| 独立立即测试创建、幂等与冻结 | `core.py`、`service.py` | 已实现；账号独立于自动成员合同需最终测试锁定 |
| direct-test prepare/publish/reconcile | `service.py`、两个 runner | 已实现，待 fake GPU/TT 回归 |
| 三态素材发布投影 | `core.py`、`service.py`、UI | 已实现，待 fixture 回归 |
| 同分钟全部 slot 预占优先 | `service.py`、`tt_post_runner.py` | 已实现：复用 `claim_recurring_run` |
| 管理 API、同源代理、权限审计 | `service.py`、`app.py` | 已实现，待 app contract 回归 |
| 多账号配置卡、状态、单目标测试 | `static/tt-post-pool.html` 及部署副本 | 已实现，待动态 UI 回归 |
| 旧 `/run-now` 兼容 | `service.py`、`app.py` | 保留；新 UI 不调用 |
| 迁移/回滚/生产只读证据 | 临时 DB、部署记录 | 待执行 |
| BUG-005 统一任务只读投影 | `core.py`、`service.py` | 已实现；同一读快照合并后筛选/统计/分页 |
| BUG-005 同源 GET 代理 | `app.py` | 已实现；仅允许 `/tasks` 及白名单查询参数 |
| BUG-005 类型标记与只读操作 | `static/tt-post-pool.html` 及部署副本 | 已实现；direct 不渲染 queue actions |
| BUG-005 自动化与文档 | 既有测试脚本、`doc/027...` | 已完成；Python `341/341`、Node `53/53` |

## 必须保持的实现合同

1. `/test-publish` 接收单素材、单目标账号、保存配置版本、幂等键、consent；目标账号可不在自动成员中，版本只冻结保存模板。
2. `/material-pool` 一次一个素材，`source_account_id` 必须在保存配置成员中。
3. 同素材活动/unknown direct-test 或 legacy queue 阻断新测试；历史 published 不阻断。auto claim 也只临时排除 direct active/unknown，direct 明确终态后 pool 可正常消费。
4. 素材投影字段为扁平的 `publication_state/publication_status/publish_count/unknown_count/attempt_count/...`，只有三态。
5. `schedules_due({"limit":N})` 在旧 claimed/unbound recovery 或新 run 的任何 creator-info 前完成全部当前 due slots 的 `claim_recurring_run` 尝试。每个 claim 独立原子；无素材不建空 run；N 只限制执行。
6. 关闭 auto-config 不解析/要求 accepted=true consent；启用时才做 consent 与实时账号校验。
7. UI 为非终态 direct-test 持久化原 key、config version 与 consent accepted_at；queued 等 submitted 响应不能提前清 key。
8. `/tasks` 是纯 GET read model；旧 `/queue` 的返回、排序和操作合同保持不变。
9. 统一任务必须使用 `task_key=automatic:<id>|direct_test:<id>` 和 `task_type=automatic|direct_test`；不能仅靠数字 ID 分流。
10. 服务端先合并、筛选和稳定排序，再计算分页前 summary 并分页；前端不得拼接两个分页响应。
11. queue 行保留 events/cancel/reconcile；direct 行只读，不调用 queue 路由，也不新增 direct-test event/detail/reconcile 管理接口。
12. 加载、筛选、翻页和生产验收只执行 GET，不得唤醒 runner、调用 GPU/TikTok 或创建 Post。

## 编译与自动化回归

```powershell
python -m py_compile features/tt_posts/core.py features/tt_posts/service.py scripts/tt_post_prepare_runner.py scripts/tt_post_runner.py app.py
python scripts/test_tt_account_settings_ui.py
python scripts/test_tt_gpu_worker.py
python scripts/test_tt_post_direct_config_core.py
python scripts/test_tt_post_links.py
python scripts/test_tt_post_pool_ui.py
python scripts/test_tt_post_prepare_runner.py
python scripts/test_tt_posts_app_contract.py
python scripts/test_tt_posts_core.py
python scripts/test_tt_posts_service.py
```

这些是仓库当前真实存在的测试文件；不得在交付命令中引用不存在的 `test_tt_post_direct_test.py`、`test_tt_post_auto_config.py`、`test_tt_post_runner.py` 或 `test_tt_post_pool_ui_runtime.py`。

BUG-005 不新增测试脚本，测试写入现有 `test_tt_posts_service.py`、`test_tt_post_pool_ui.py`、`test_tt_posts_app_contract.py`，必要时补 `test_tt_posts_core.py`。必须先执行 T01-T12 targeted，再执行上述 9 个脚本全量回归；基线 `334/334` 不能替代增量验证。

## 迁移验证

1. 只在 DB 副本启动应用迁移两次。
2. 两次后表/索引相同，`PRAGMA integrity_check=ok`。
3. 旧 schedule/pool/queue/run 行、版本和计数保持不变；单例配置和 direct-test 表为空。
4. schema 只出现两张新表及 direct-test 的六个实际索引；其中 active/unknown material partial unique index 负责跨请求并发互斥，不得出现 `tt_post_direct_test_event`、`tt_post_auto_due` 或额外 account index。
5. 在副本验证 uniform/mixed version-0 投影、mixed 停用首次保存、下一版本启用及事务回滚。

## 完成条件

- 上述全部测试退出码 0；测试用例 D/P/C/S/M/N/T 全通过。
- fake GPU/TT 证明没有真实 Post；生产只读验收不发送写请求。
- SA 代码评审确认 API 名称、字段、两张表、同分钟顺序、统一任务分页与操作隔离均与实现一致。
- GitHub commit、release SHA、DB 路径/备份、服务/timer 状态和回滚点齐全后才允许部署。
- BUG-005 的 T01-T12、旧 `/queue` 快照回归和生产只读 0 副作用证据已齐全，状态为已上线、已关闭。
