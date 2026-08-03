# 027 TT 独立立即测试与多账号原子配置

## 当前状态

- 阶段：实现已进入工作树，等待完整自动化回归、SA 代码复核与 GitHub-first 部署。
- 发布结论：完整测试证据形成前不得发布。
- 变更边界：立即测试、素材发布状态、自动发布原子配置、多账号同分钟排期。
- 安全边界：本需求的开发及生产验收不得创建真实 TikTok Post，不得保存或改动生产自动发布配置。

## 锁定决策

1. “立即发布测试”使用独立异步任务，不从自动素材池选材，也不修改自动池状态、顺序或归属。
2. 运营必须明确选择一个已校验素材 ID 和一个测试目标账号。已发布过的素材允许再次测试。
3. 每个测试任务生成全新的 GPU prepare job；GPU prepare/publish 接口和 publish ledger 规则不改，不覆盖旧 ledger。
4. 同一幂等键只对应同一任务；同一素材存在活动中或 `unknown` 的自动/测试发布时，禁止用新幂等键绕过，必须先完成核对。
5. 素材发布投影只有 `published|unknown|unpublished`；`consumed`、失败或取消不等于已发布，活动任务仍显示 `unpublished` 并通过尝试次数反映。
6. 描述模板、自动发布总开关/每日时间、自动发布账号集合是一个配置，只使用一个乐观锁版本原子保存。
7. 多选账号表示加入自动发布配置；每条自动池素材仍明确归属一个账号，不复制、不广播、不静默取第一个账号。
8. 多账号同一分钟到期时，先对所有 due slot 调用现有 `claim_recurring_run`；每个成功预占独立原子写入现有 run 并保留精确 FIFO 素材。所有预占尝试结束后才允许第一次 `creator_info` 网络调用，不新增 due 表。
9. 旧逐账号排期先以只读兼容视图加载；首次保存前必须明确处理时间差异，禁止把旧时间并集静默应用到每个账号。
10. 立即测试账号为独立显式单选，可不属于自动发布账号集合；配置版本只用于冻结已保存描述模板。自动素材入池账号仍必须属于自动发布集合。
11. 旧 `/run-now` 兼容接口保留，但页面的立即测试只调用 `/test-publish`，不得再从自动池取材。
12. 正常回滚只回退代码和静态资源，保留 SQLite 新表、新任务、未知结果、GPU ledger、manifest、COS 对象和历史发布事实。

## 文档索引

| 文档 | 内容 |
| --- | --- |
| [requirements.md](requirements.md) | 需求分析、业务规则、交互、技术设计与验收标准 |
| [sa-review.md](sa-review.md) | SA 需求与方案评审结论 |
| [dev-plan.md](dev-plan.md) | 实现拆分、验证命令与依赖 |
| [api-doc.md](api-doc.md) | 管理端与内部接口合同、错误码、兼容策略 |
| [migration.sql](migration.sql) | 当前实现的 additive schema 评审镜像，不可直接在生产执行 |
| [test-cases.md](test-cases.md) | QA 用例与无副作用验收矩阵 |
| [sa-test-review.md](sa-test-review.md) | SA 对测试覆盖的评审 |
| [sa-code-review.md](sa-code-review.md) | 待实现后的代码评审门槛 |
| [deploy.md](deploy.md) | GitHub-first 部署、迁移与只读生产验收 |
| [rollback.md](rollback.md) | 不覆盖 SQLite/ledger 的回滚方案 |
| [bugs/](bugs/) | 一个缺陷一个文件的现状与修复验收 |
| [test-report.md](test-report.md) | 当前未执行状态及最终报告模板 |

## 完成定义

- 所有 P0/P1 用例通过，开放 P0/P1 缺陷为 0。
- 测试证明重复测试不会消费自动池，自动排期不会重复使用已经由自动流程消费的素材。
- 版本冲突、非法账号、`unknown`、进程崩溃等失败路径均证明 0 部分写入、0 真实 Post、0 pool/queue/ledger 非预期变更。
- 生产只读验收前后，自动配置版本和值、queue/run/pool 数量与状态、GPU ledger 数量、已知 Post 基线完全一致。
