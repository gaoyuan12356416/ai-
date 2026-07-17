# 009.ad-control-v3-live-execution 需求与技术设计

## 背景

V3 动态规则组已支持产品 + 优化师范围、Campaign/Ad Set/Ad 条件试算和观察日志，但线上尚未连接 Meta 写接口与独立调度器。用户需要在不影响 V2 的前提下，使 V3 真正暂停或复制命中对象，并保留可追溯的发布数据和复制关系。

## 目标

- Facebook 三层对象真实暂停。
- Facebook 三层对象按承载策略真实复制；Campaign 复制全部子 Ad Set/Ad。
- 复制先 PAUSED，created_data 与 lineage 成功后再由独立开关激活。
- 手动执行与每分钟账号时区调度均可用；观察模式保持零 Meta 写入。
- 任一步失败可停止新动作、隔离已创建对象并按发布检查点回滚代码/配置。

## 范围

### 包含

- `pause`：Campaign、Ad Set、Ad。
- `copy`：Campaign `deep_copy_campaign`；Ad Set `same_campaign/new_campaign`；Ad `isolated_adset/isolated_campaign`。
- `same_adset` 保留配置兼容，但因无法设置独立预算而在写入前明确阻断。
- 预算：实际 CPI × X、固定 CPI × X、来源预算 × 百分比。
- MIN_ROAS 出价按百分比提高/降低；不兼容时写前阻断。
- MySQL intent、FB created_data 镜像、lineage；不更新来源库 created_data。
- 手动正式执行与账号时区 scheduler。

### 不包含

- TikTok 写接口。
- 静默改变竞价策略。
- 自动删除 Meta 对象或用数据库回滚撤销已创建对象。
- 将复制结果写回 `kunlunads_dev`。

## 用户故事 / 业务规则

1. 优化师保存正式规则并试算后，输入 `EXECUTE_LIVE_RULE_GROUP` 可手动执行最新命中结果。
2. 启用正式调度需有效试算、计划、`ENABLE_LIVE_MODE` 和 runner live release 开关。
3. 同一对象同时命中暂停与复制时只执行暂停。
4. 复制按账号本地自然日执行单规则、规则组、用户和部署硬上限；同一来源在冷却期内不重复。
5. 每个外部动作前验证对象账户和父级；复制前要求 Meta Ad 与 created_data 一一映射。
6. 网络超时后的 POST 不重试；intent 进入隔离，避免重复创建。
7. 急停/停用/配置变化后不再启动下一个目标。

## 交互与流程

规则列表为正式模式且服务端能力开放时显示“执行”。点击后必须输入完整确认短语。执行日志展示成功、跳过、失败和 Meta 写次数。调度器每分钟触发一次，只处理当前账号本地时间到期的候选。

复制顺序：

1. 校验 created_data 镜像结构、来源映射、Token、账号、预算和 ROAS。
2. 事务预占 intent/额度。
3. 逐层浅复制，所有新对象保持 PAUSED。
4. 回读 source_* 映射、状态、预算和出价。
5. 同一事务写 created_data（每个 Ad 一行）、lineage、intent 状态。
6. 激活开关打开时依次激活 Campaign、Ad Set、Ad，再回写最终状态。
7. 任一步失败，尽力 PAUSE 已知新对象并隔离 intent。

## 技术设计

### 影响模块

- `features/ad_control_v3/live_execution.py`：Graph、Token、暂停、复制、额度、落表和隔离。
- `features/ad_control_v3/service.py`：能力开关、手动执行、调度执行和审计快照。
- `features/ad_control_v3/scheduler.py`、`scripts/ad_control_v3_runner.py`：账号时区计划与幂等 tick。
- `features/ad_control_v3/assets/app.js`、`routes.py`：真实执行入口。
- `doc/.../sql/003_enable_fb_live_pause_copy.sql`：三张 ads_ai 表。

### 数据结构

- `ads_ai.ads_facebook_auto_created_data`：完整镜像来源 56 列；每个新 Ad 一行。
- `ads_ai.ad_control_v3_copy_intent`：唯一 idempotency key、额度日期、状态和结果。
- `ads_ai.ad_control_copy_lineage`：来源库/行/Meta ID 到新 created_data/Meta ID 的映射。

### API / 接口

- `POST /api/ad-control/v3/rule-groups/{id}/execute`
- 现有 `meta` 新增 `can_live_execute`、pause/copy/scheduler 能力。
- 现有 `enabled` 对正式模式增加确认和 runner release 校验。

### 异常与边界

- 单次最多 50 个 live 目标、10 个复制目标、100 个 Ad Set、500 个 Ad。
- 直接 deep copy 不使用：真实 Meta 测试已证明对象数超过 3 会报 1885194。
- Ad creative 复制传入来源单项 creative features，并移除废弃 `standard_enhancements`。
- `same_campaign` 的 CBO、`isolated_adset` 的 CBO、`same_adset` 独立预算均写前阻断。

## 验收标准

- 观察/试算 Meta 写次数为 0。
- 三层暂停均回读 `configured_status=PAUSED`。
- Campaign 复制产生的 created_data 行数严格等于新 Ad 数。
- 结构/落表/映射失败不激活；相同 intent 不二次调用复制。
- 调度严格按账号时区、窗口、固定时间/间隔执行。
- V2 路由、cron 和数据保持不变；全量自动调控回归通过。

## 风险与待确认

- ACTIVE Canary 会产生真实投放风险，部署验证仅创建 PAUSED 对象；ACTIVE 由业务规则首次执行验证。
- Meta API 字段可能漂移；通过 Graph v25.0 回读和错误隔离防止静默成功。

## 变更记录

- 2026-07-17：实现 FB V3 真实暂停、分层复制、ads_ai 落表、手动执行和账号时区 runner。
