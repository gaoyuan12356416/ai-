# SA 评审意见

## 结论

修订后有条件通过。本期完成规则模型、Campaign 观察/试算、候选与复制参数校验，但在持久化方案确定前，Campaign 正式复制必须在任何 Meta POST 前返回 `copy_persistence_not_configured`。产品从规则维度移除但保留为候选内部元数据；Ad 仅可保存配置，启用、候选、试算、runner 和正式执行全部后置。

## 问题清单

| 编号 | 严重级别 | 位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| SA-001 | P0 | 概念模型 | Campaign、关闭/复制、观察曾被混为动作 | 分拆 object_level/action/run_mode | 已解决 |
| SA-002 | P0 | 分布式一致性 | Meta 与持久化无法同事务 | 本期禁止生产复制；未来通过 intent、PAUSED 隔离和分阶段恢复解决 | 已采纳并后置 |
| SA-003 | P0 | created_data | 写入格式尚待用户指定 | 不建表、不写入、不固化字段映射 | 已采纳并后置 |
| SA-004 | P0 | 关联语义 | 复制父子关系必须可追踪 | 本期仅保留设计约束，随持久化方案实现 | 已采纳并后置 |
| SA-005 | P0 | 权限 | API 可伪造 owner | owner 只取登录 session，所有读写校验本人 | 已采纳 |
| SA-006 | P0 | 放量风险 | 代码上线可能误复制 | Campaign copy 总熔断默认关闭且持久化前置固定失败；Ad 熔断仅为后续占位，本期不能通过开关启用 | 已采纳 |
| SA-007 | P1 | 账号范围 | 删除产品后账号可能跨多个产品 | 账号聚合，产品仅作候选内部元数据和 token 路由 | 已采纳 |
| SA-008 | P1 | 旧规则兼容 | 新模型可能改变现有 pause | SQLite 兼容迁移，旧组保留关闭行为且不自动复制 | 已采纳 |
| SA-009 | P0 | 范围修订 | 复制结果 ads_ai 写入被明确跳过 | 生产 copy 前置失败关闭；仅 Campaign observe/preview 继续交付，Ad 仅保存配置 | 已采纳 |
| SA-010 | P0 | 旧数据归属 | owner 和 created_by 双空的启用规则无人可管理，但 runner 可能继续执行 | 兼容迁移自动禁用并急停；上线前备份并只为核实过的当前用户精确赋 owner | 已采纳 |
| SA-011 | P0 | 迁移幂等 | `product=''` 的 V2 账号组可能在重启时被当成 legacy 再次迁移 | legacy 选择条件限定 `product <> ''`，专门回归重复 ensure | 已采纳 |
| SA-012 | P0 | 状态机/preview | save payload 或损坏的过期时间可能绕过专用启用与 preview 门禁 | save 不写 enabled；损坏 expires_at 返回 `preview_invalid` 并且零 Meta 写 | 已采纳 |
| SA-013 | P2 | 并发/性能 | 每次 pause 的最终一致性锁跨越 Graph GET/POST，可阻塞其他 SQLite 写 | 本期优先保证 stale preview 不写；记录约 60 秒最坏阻塞边界，暗发布监控耗时/排队，异常先停 runner/live 规则再回滚 | 已接受，需生产观察 |
| SA-014 | P0 | 共享 monolith 部署 | 线上新版 action-log 已有独立 writer/reader 和超时/并发/重试保护，旧部署补丁可能回退这些安全契约 | 补丁显式保留 writer/reader 分离、固定 action-log 库表、现行超时/并发上限与无立即 upsert 重试；部署前对 current-live fixture 执行 check/apply/幂等与契约断言 | 已采纳，上线前必验 |

## 决策记录

- 隔离 Stub 适配器按 Meta Campaign copy 契约使用 `POST /{campaign_id}/copies`，并固定 `deep_copy=true,status_option=PAUSED`；该适配器本期不接入生产 app/runner。
- Copy 响应只作为新 Campaign 入口，Ad Set/Ad 映射必须回读 `source_adset_id`/`source_ad_id` 验证，不能假定响应含完整映射。
- 本期不建立 FB/TT 复制结果表，也不进行 copied created_data/lineage/intent DDL/DML；既有 `ads_ai.ad_control_action_log` 只是执行审计，不是复制结果落表。DDL 环境问题已修复不会自动扩大本期范围，后续建表/写入仍等待用户明确授权。
- Campaign 观察模式不创建生产 copy intent、不预占真实额度；可写 SQLite preview/runner 状态及既有 `ads_ai.ad_control_action_log` 审计，但不写 copied created_data/lineage/intent。Ad 观察 runner 本期不可启用。
- 旧聚合组迁移必须显式提交 `migrate_from_group_ids` 并在同一事务中收敛旧行；迁移前 `partial_enabled` 必须如实显示。旧 `action=observe` 显式转为 `run_mode=observe` + `action=pause`，未知 action 拒绝保存。
- ownerless legacy 组默认禁用+急停；新 V2 账号组不参与 `product` 维度的 legacy 重运移。普通 save 只保存配置，不承担 enabled 状态转换。
- preview 过期时间损坏与 stale/missing/expired 一样 fail-closed；不能将解析异常当作“永不过期”。
- 共享 monolith 的补丁验收必须包含 current-live fixture；不能只用仓库旧基线证明安全。如补丁改变 action-log writer/reader 分流、连接/读写超时、live worker 上限或 runner 状态更新不立即 upsert 重试契约，必须停止发布。
- 未来正式复制仍须遵守“PAUSED 创建 -> 一一映射 -> 持久化 -> 回读校验 -> 激活”，但本期生产路径不会调用该状态机。

## PM 修订确认

2026-07-15 用户已确认修正版方案并要求实施，随后明确本期跳过复制结果 ads_ai 写入。用户后续确认 DDL 问题已修复，仅表示后续获得明确授权时可再尝试，不改变本轮边界。此次只能上线 Campaign observe/preview 与既有 pause；Ad 仅保存配置，任何复制 Canary 后置。
