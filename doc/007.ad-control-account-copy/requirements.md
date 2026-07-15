# 007.ad-control-account-copy 需求与技术设计

## 背景

现有 FB 自动规则调控按产品和账号筛选 Campaign，规则命中后只支持关闭。业务需要让优化师在同一规则组中配置账号范围、调控对象、运行模式和动作，并在优秀 Campaign 命中时安全复制账户内现有结构，减少重复发布。

## 目标

- 不新增菜单，升级“规则组管理”。
- 删除规则组的产品选择，规则范围只由当前用户和广告账号确定。
- 将调控对象、命中动作、运行模式拆成独立维度。
- 第一阶段完成 Campaign 关闭、复制规则配置、候选扫描和安全编排；Ad 仅允许保存配置，候选扫描和正式执行均待第二阶段单独实现。
- 按 2026-07-15 最新范围，本期跳过 copied created_data/lineage/intent 的 `ads_ai` 建表与写入；既有 `ads_ai.ad_control_action_log` 是执行审计链路，不是复制结果落表。即使 DDL 环境问题已修复，也只表示后续获得用户明确授权时可再尝试；本轮范围不因此扩大。在复制结果持久化方案确认前，正式复制在任何 Meta POST 之前 fail-closed。
- Campaign 观察/立即试算可完整验证“本来会复制哪些对象”；Ad 本期不产生候选。现有 Campaign 正式关闭动作继续可用。

## 范围

### 包含

- 规则组契约包含 `object_level=campaign|ad`、`run_mode=observe|live`、账号范围、执行计划、额度和候选策略；但本期 Ad 只能以 disabled/observe 保存配置，不能启用或切正式执行。
- 规则动作：`pause|copy`，复制参数仅在 `copy` 时生效。
- 新规则组默认 `enabled=false`、`run_mode=observe`，进入正式模式需要显式二次确认。
- Campaign 指标扫描、动作冲突消解、Top N、时区、时间窗、冷却期和每日额度。
- Campaign 复制参数和安全编排：CBO/ABO 预算层级、兼容 ROAS 出价检查、PAUSED 创建与有界完成轮询均以依赖注入模块和 Stub Meta 测试固化。
- 账号范围、原始 FB 发布数据候选、观察日志和复制熔断状态。
- Campaign 正式复制的持久化前置校验：未配置时返回 `copy_persistence_not_configured`，且 Meta 写调用数必须为 0。

### 不包含

- TikTok 复制和 TT 目标表写入。
- 任何复制结果 `ads_ai` 建表、迁移、copied created_data/lineage/intent 写入或联合数据源改造；DDL 环境虽已修复，但后续仍须由用户明确授权并确认写法后另立需求。既有 `ads_ai.ad_control_action_log` 审计链路保持原样，不得将其当作 copied created_data/lineage/intent 已实现的证据。
- 本期不开放 Campaign 或 Ad 的正式复制。Campaign copy 固定返回 `copy_persistence_not_configured`；Ad 仅可保存配置，启用、候选扫描、试算、runner 和正式执行均返回 `phase_not_enabled`；二者均零 Meta copy 写。
- 静默改变来源竞价策略。
- 删除或覆盖 `kunlunads_dev` 来源行。
- 自动删除 Meta 已创建对象；异常对象只能精确 PAUSE 和隔离。

## 用户故事 / 业务规则

1. 优化师只能查看、创建、修改、启停和删除自己的规则组，API 不接受代他人设置的 `owner_user_id`。
2. 一个规则组只有一个对象层级；每条规则只有关闭或复制动作。
3. Campaign 观察模式持续扫描并记录本来会执行的动作，不调用 Meta 写接口，也不写 copied created_data/lineage/intent；Ad 规则组只能保存，不能启用观察 runner。
4. 同一对象同时命中多个规则时，关闭优先；未执行规则记为 `shadowed_by_rule`。
5. 每条复制规则可配置剧目范围、固定时间/间隔、账号时区允许窗口、单规则/用户/部署日额度、冷却期、全部候选/每账号 Top N、预算和 ROAS 调整。
6. 默认额度：单规则 1 次/天、当前用户 10 次/天、部署硬上限 50 次/天、同一来源 1 天内不重复复制。
7. 排序默认 ROAS 降序、消耗降序、对象 ID 升序，保证结果稳定。
8. 复制后的目标状态配置可设 ACTIVE，但本期持久化前置未实现，因此正式复制不得进入 Meta 创建阶段。
9. 来源不是兼容的 ROAS 出价模式时跳过该候选，不自动更换策略。
10. 普通保存接口不得通过 payload 中的 `enabled=true` 越过专用启用流程；新组和行为配置变更后均保持禁用，只能经有效 preview 和二次确认后由专用启用接口打开。
11. 既无 `owner_user_id` 也无 `created_by` 的 legacy 规则组无法安全归属，兼容迁移必须将其 `enabled=0` 并设置急停，等运营人员基于备份核实后显式分配 owner。
12. 新 V2 账号维度规则组的 `product` 为空，只有 `product` 非空的历史绑定才能进入 legacy fan-out 兼容迁移，防止重复启动时误分类。
13. preview 的过期时间为必须可验证的安全字段；缺失、格式损坏或已过期均 fail-closed，不允许继续执行 Meta 写操作。

## 交互与流程

规则组顶部配置当前用户、账号范围、调控对象、运行模式、执行计划和额度；规则卡片配置筛选条件、动作，以及复制动作专属参数。“立即试算”是一次性预览，不改变持续运行模式。切换正式执行时前端确认，后端同时校验 `live_mode_confirm=ENABLE_LIVE_MODE`。

本期运行链路：

1. Campaign 观察模式或“立即试算”扫描账号和指标，完成规则命中、关闭优先、剧目范围、Top N、时间窗和额度预判；Ad 直接返回 `phase_not_enabled`。
2. `pause` 在正式模式沿用现有预览确认和 Meta PAUSED 链路。
3. Campaign `copy` 在观察模式记录 `would_copy`；正式模式先检查持久化前置并返回 `copy_persistence_not_configured`，不创建生产 intent、不调用 Meta、不写 copied created_data/lineage。SQLite 规则/预览状态和既有 `ads_ai.ad_control_action_log` 审计链路不在“零复制结果写入”范围内。
4. 深复制、映射、预算/ROAS 和恢复状态机只通过 Stub Meta 测试，为后续接入用户指定的写入方式保留稳定接口。

## 技术设计

### 影响模块

- `app.py`：SQLite schema 兼容迁移、当前用户权限、规则组 API、预览与执行接线。
- `scripts/ad_control_rule_runner.py`：按计划触发观察/正式动作和幂等续跑。
- `features/ad_control_copy_engine/`：规则归一化、冲突消解、额度计算和隔离的 Campaign 复制编排契约。
- `static/ad-control-*`：规则组配置 UI。

### 数据结构

- SQLite `ad_control_rule_group` 增加 `owner_user_id`、`object_level`、`run_mode`；旧 pause 组迁移为创建者拥有、Campaign、正式模式，保持现有关闭行为。
- 仅有可证实的 `created_by` 可作为 legacy owner 回填来源；双空的 ownerless 组自动禁用和急停。生产升级前必须备份 SQLite，列出 ownerless 与非空 `created_by` 行，只对已核实的当前用户做精确 owner 迁移。
- legacy 识别条件限定为历史 `product <> ''` 绑定；账号维度 V2 组不参与该迁移。
- 旧聚合组编辑时，前端通过 `migrate_from_group_ids` 明确提交旧底层组 ID；后端在同一 SQLite 事务中保存新组并禁用、软删除旧组。迁移前列表必须保留 `partial_enabled`，不得把部分启用误报为已禁用。
- 旧 `action=observe` 不再作为动作保存：编辑时显式迁移为 `run_mode=observe` + `action=pause` 并提示用户；其他未知 action 一律拒绝，不能静默改成 pause。
- 本期不新增或修改任何复制结果 MySQL 表，不写 copied created_data/lineage/intent；既有 action log 链路不属于本次后置范围。
- 复制 intent/lineage 的生产持久化方式随 `ads_ai` 写入方案一并后置；隔离模块单测使用临时 SQLite，不接入 app/runner。
- 线上既有 action-log 安全契约必须保持：写节点与读节点分离，固定 `ads_ai.ad_control_action_log`，保留现行连接/读写超时、并发上限和 runner 状态更新不立即 upsert 重试的保护。部署补丁必须同时对旧基线与当前线上副本（current-live fixture）做 check/apply/幂等验证，任一不通过则不得部署。

### API / 接口

- `GET /api/ad-control/rule-groups`：仅返回当前用户拥有的规则组。
- `POST /api/ad-control/rule-groups`：新建或按 `group_id` 更新（upsert）规则组；拒绝 owner 伪造和非法动作；旧聚合组迁移可携带 `migrate_from_group_ids`。该接口忽略并拒绝任何通过保存开启 `enabled` 的绕过，行为变更会清空旧 preview 并强制禁用。
- `DELETE /api/ad-control/rule-groups/{id}`：软删除本人规则组并禁用后续执行。
- `POST /api/ad-control/rule-groups/{id}/enabled`：启停；正式模式必须已有有效试算。
- `POST /api/ad-control/rule-groups/{id}/preview-live`：立即试算，永不调用 Meta 写接口或复制结果写入；仍会写 SQLite preview/审计元数据。
- `POST /api/ad-control/rule-groups/{id}/execute-live`：执行预览；观察模式强制 dry-run，正式复制在本期返回持久化未配置且零 Meta 写。
- Runner 复用以上领域函数，不通过 HTTP 回环。

### 异常与边界

- Meta 复制编排的崩溃恢复、映射和激活补偿仅由 Stub 测试验证；生产入口在 Meta 写前被持久化前置门禁阻断。
- 复制额度按广告账号时区自然日计算；无时区时 fail-closed，不按服务器时区猜测。
- 剧目范围依赖明确的 created_data/发布队列映射；映射缺失或歧义时必须跳过，不能从 Campaign 名称猜测。
- 正式 Campaign pause 为防止 preview 在 Graph GET/POST 之间失效，当前实现持有全局 `JOB_DB_LOCK` 和 SQLite `BEGIN IMMEDIATE` 跨越对象状态回读及写入。按两次 30 秒 Graph 超时计算，单 Campaign 最坏可阻塞同进程其他 job SQLite 写入约 60 秒；这是当前为正确性接受的 P2 取舍，上线必须监控 API/runner 耗时、job 写入排队和 Graph 超时，异常时可先停 ad-control runner/禁用 live 组再回滚应用。

## 验收标准

- 观察模式执行时 Meta 写调用数为 0，且本期没有任何复制结果 SQL 写入。
- 关闭与复制同时命中时只关闭，其他命中记录 `shadowed_by_rule`。
- Campaign copy 在观察/试算中正确显示候选、预算和 ROAS 预判；正式调用返回 `copy_persistence_not_configured`，Meta 写调用数为 0。
- Ad 对象层级可保存配置，但启用、试算、runner 和正式执行均返回 `phase_not_enabled`，不得把 Campaign 候选伪装成 Ad，Meta 写调用数为 0。
- 复制熔断可独立关闭 copy，现有 pause 规则不受影响。
- 所有自动化测试通过；线上观察要求 0 次 Meta copy、0 次复制结果表写入。既有 `ads_ai.ad_control_action_log` 可继续记录执行审计；PAUSED Canary 延后到落表方式确认后的下一需求。

## 风险与待确认

- 复制结果 `ads_ai` 写入规则、表结构和 lineage 契约以用户后续说明与明确授权为准；本期即使 DDL 环境已可用，也不预先建表或固化写入实现。既有 `ads_ai.ad_control_action_log` 不是复制结果落表，不在该后置范围内。
- Meta `/copies` 在不同 Campaign 结构和 Graph 版本下返回映射字段可能不同，后续 Canary 前必须以真实 PAUSED 对象核验。
- 共享 monolith 线上没有 Git 元数据，部署必须从当前线上副本做窄合并和原子替换，不能整份覆盖 `app.py`。

## 变更记录

- 2026-07-15：初版曾计划账号维度、Campaign 第一阶段和 ads_ai 分渠道落表；该落表计划已被下一条范围调整废止。
- 2026-07-15：用户调整范围，本期跳过复制结果 ads_ai 写入；正式 copy 在 Meta POST 前 fail-closed，Campaign 规则组与观察能力继续实施，Ad 仅保存配置。
- 2026-07-15：安全评审补充 ownerless legacy fail-close、V2/legacy 迁移边界、保存不可绕过启用、损坏 preview fail-close 以及 `JOB_DB_LOCK` 跨 Graph 请求的 P2 运行取舍。
- 2026-07-15：用户确认 DDL 问题已修复，可在后续做到该范围时再尝试；本轮仍不建、不写 copied created_data/lineage/intent，等待下一次明确授权。同步将 current-live action-log writer/reader 兼容验证列为部署前 P0 门禁。
