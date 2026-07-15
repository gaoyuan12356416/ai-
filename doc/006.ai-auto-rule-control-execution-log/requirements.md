# 006.ai-auto-rule-control-execution-log 需求与技术设计

## 背景

现网规则组一次最多执行 200 个目标。旧逻辑按账户和 Campaign 排序后直接截取前 200 条，账户之间不均衡；单账户连续请求也容易触发 Meta `code=4 / error_subcode=5044001`。日志页只展示“200 目标/成功/跳过/失败”，无法区分规则命中总量、本批计划量和后续待处理量。

旧执行链在 Meta 成功后还尝试更新 `kunlunads_dev`。生产账号对该库只有只读权限，因此产生“同步业务库失败”警告。该警告不应污染 Meta 执行结果，调控审计应改存到有写权限的 `ads_ai`。

## 目标

- 保留单轮安全总上限 200，但按账户公平选批，单账户单批不超过 20。
- 不同账户最多 4 路并行，同一账户严格串行。
- Meta 应用级限流触发后立即熔断本批尚未发出的请求，并由后续 runner 续跑。
- 将调控审计持久化到 `ads_ai.ad_control_action_log`，本地 SQLite 仅作为 outbox/回退。
- 日志页明确展示“扫描 → 候选 → 命中 → 本批计划 → 待后续处理”，并将 Meta 结果与日志存储状态分开。
- 日志主视图按业务日汇总同一规则的定时续跑链；底层 action 行保持不变并可逐批追溯。
- 将“待续跑”“已执行待零目标复核”“历史未完成/执行受阻”拆成不同状态，不再统一显示为“部分完成”。

## 范围

### 包含

- 新规则组 `ad_control_rule_group` 的 live preview、live pause、定时 runner 和执行日志页面。
- 现有 SQLite 日志一次性回填到 `ads_ai`。
- 日志列表轻量查询、目标明细按需加载、日期按 Asia/Shanghai 自然日筛选。
- 按业务日聚合的轻量读模型、批次清单和原始批次切换；列表仍不加载 `results_json`。
- Meta Graph 限流、瞬时错误、永久配置错误的分类与续跑状态。

### 不包含

- 自动重启 Campaign；规则组仍只执行 pause。
- 修改业务投放表状态；Meta Graph 是执行事实来源，SQLite object state 只用于调控中心内部状态。
- 清理或改变历史调控规则、账户池、token 配置。
- 当前未启用的旧 `ad_control_rule` 执行器改造。上线前已验证生产旧规则数量为 0。

## 用户故事 / 业务规则

1. 运营查看一条日志时，应直接知道本轮扫描、白名单候选、规则命中、本批计划及剩余数量，而不是把 200 误解为总目标。
2. 同一账户每批最多选择 20 条；有多个账户时采用轮询选取，避免大账户长期占满 200。
3. 同一账户内请求串行；账户间并发上限 4。
4. `code=4` 或 `error_subcode=5044001` 视为应用级可重试限流，设置共享熔断器；尚未调用 Meta 的项目记为 `deferred`，不计入真实失败。
5. `not_active/already_paused/not_pause_target` 为终态跳过；缺 token、越过产品白名单、owner 不一致为受阻，必须 fail closed。
6. 当本批执行完成但尚未做零目标复核时，事件保持 `partial`；下一次 fresh preview 确认无目标、无错误后单独记录 verification 日志并完成事件。
7. 新事件的续跑次数从 1 开始；同一 partial 事件可跨午夜续跑，最多 24 次。
8. `ads_ai` 写入失败不改变 Meta 成功/失败计数；API 标记 `sqlite_fallback`，后续可安全回填。
9. 主列表的聚合键固定为业务日期、产品、规则标识、动作、对象层级和 dry-run/正式模式；`event_key` 只用于提取原业务日期和批次追溯，同日同规则即使有多个 event 也只显示一张日卡，跨午夜续跑仍归原业务日。
10. 缺少 `event_key` 的历史 scheduled 日志，仅在 `actor_user_id=ad_control_rule_runner` 且规则标识完整时按本地业务日回退聚合；人工操作或规则标识缺失时保持单条，避免误合并。
11. 日汇总先按 `event_key` 分事件链并取各链最终批次，再按“状态异常/受阻/失败 > 历史未完成 > 待续跑 > 待复核 > 完成”保守归并；只有当天所有事件链都满足 `executed + remaining=0` 才显示完成，未闭环事件的最终剩余数相加。
12. 最新 partial 批次超过 3 小时仍没有完成记录时显示“当日未完成”，不能继续显示成正在续跑；runner 每5分钟续跑、最多24次（理论2小时），3小时窗口额外保留1小时调度/限流缓冲。
13. `scanned/candidate/matched/remaining` 是快照，不跨批次求和；列表显示首轮命中、批次数、执行尝试次数和最终剩余。成功/跳过/失败的累计必须明确标注为“尝试”，不能称为去重目标。
14. 批次详情继续按单 action 懒加载，保留 action_id、preview_id、批次状态和原因；不在列表阶段读取或拼接全天大 JSON。

## 交互与流程

```text
fresh preview
  -> 汇总扫描/候选/命中
  -> 按账户 round-robin 选取 <=200、每账户 <=20
  -> 账户间最多4并行、账户内串行执行
  -> Meta结果 + deferred/remaining 统计
  -> SQLite outbox
  -> ads_ai upsert
  -> partial 时下个 cron fresh preview
  -> 0目标且0错误时写 verification，事件完成
```

## 技术设计

### 影响模块

- `features/ad_control_execution_log/service.py`：批次选择、错误分类、汇总、MySQL DDL/CRUD。
- `deploy/apply_ad_control_execution_log_fix.py`：对生产复合版 `app.py` 做窄范围、可重复补丁。
- `scripts/ad_control_rule_runner.py`：续跑状态机、preview failure/verification 日志。
- `scripts/migrate_ad_control_action_logs.py`：SQLite → `ads_ai` 回填，默认跳过已存在 action，避免覆盖 runner 最终状态。
- `static/ad-control-pages.js|css` 与 7 个 ad-control HTML：日志 UI 和统一 cache buster。

### 数据结构

新增 `ads_ai.ad_control_action_log`，以 `action_id` 为主键。保存业务维度、runner 状态、五段流程计数、Meta 结果计数、可重试/受阻/剩余计数、criteria/results JSON 及时间索引。建表 SQL 见 `001_create_ad_control_action_log.sql`。

SQLite `ad_control_action` 保留，作为先落本地的 outbox 和 MySQL 不可用时的读取回退，不再作为日志页的首选数据源。

### API / 接口

- `GET /api/ad-control/actions`：列表默认不返回 `results_json`；`view=daily` 返回业务日汇总，`view=raw` 保留原始批次兼容视图。
- `GET /api/ad-control/actions/{action_id}/targets`：展开时加载目标与原始结果。
- `POST /api/ad-control/rule-groups/{id}/preview-live`：返回 scan/candidate/matched/batch/remaining。
- `POST /api/ad-control/rule-groups/{id}/execute-live`：返回 retryable/blocked/deferred/remaining 与 log store。

### 异常与边界

- MySQL 不可用：执行不回滚，列表回退 SQLite，页面明确提示。
- Graph owner 缺失或不一致：不写 Meta，记 `account_owner_mismatch`。
- 应用级限流：最多保留当时已在途的 4 个请求，后续目标全部 deferred。
- 旧日志缺少新流程字段：日志版本 1 显示未知为 `--`，不伪装成真实 0。
- 日期筛选：浏览器提交 Asia/Shanghai 业务日期；daily 模式将轻量查询窗口前后各扩一天，再按 `business_date` 二次过滤，保证跨午夜续跑不被查询阶段丢掉。
- 大字段：列表不读取 `results_json`，只在用户展开详情时读取。
- 聚合窗口：daily 模式最多读取 1000 条轻量 action、按 action_id 去重再聚合；若窗口命中上限，丢弃截断边界业务日及更早的全部日组并返回截断提示，不能展示错误的半条日汇总。raw 模式仍保持最多 200 条。
- 历史迁移：`log_version=1 + event_key/runner_reason 为空` 的状态仅作兼容推导；末批含错误且无后续完成时标记历史未完成，不伪装成活跃续跑。

## 验收标准

- 927 个命中目标不会表现为单次“只有 200 个目标”；页面显示命中 927、本批计划 200、剩余 727（以实际数据为准）。
- 任一批次每账户最多 20，全部账户合计最多 200，不同账户最多 4 路并行。
- 模拟 code4/subcode5044001 时只计 1 个真实失败，未发请求项计 deferred，事件为 partial 并允许续跑。
- `ads_ai.ad_control_action_log` 能创建、写入、按 action_id 幂等更新、列表与详情读取。
- 数据库日志写失败时 Meta 结果计数不变，页面显示 SQLite 回退。
- 历史 SQLite 日志回填后数量一致；二次回填不覆盖已有 runner 状态。
- 7 个页面统一引用新 JS/CSS 版本，日志列表无大字段预加载。
- 单元测试、JS 语法、生产同源补丁演练、线上 dry-run/读接口回归全部通过。
- 2026-07-15 的两批显示为一张“当日执行完成”卡；2026-07-14 的四批显示为一张“当日未完成”卡，保留限流失败原因和19个最终剩余。
- daily 模式下 `limit` 在聚合后生效；原始批次仍可展开并逐批加载目标，底层16条历史 action 不更新、不删除。
- 200+200+186 等多批不得显示为三张主卡；跨午夜同一 event_key 不拆日，不同规则/人工执行不得误合并。

## 风险与待确认

- `results_json` 会持续增长；本次先建立时间索引，建议后续按 180 天归档/清理并监控表容量。
- 生产 `app.py` 是共享复合版，部署必须先备份、对真实文件运行 `--check` 和 diff，不允许整文件用仓库版本覆盖。
- `63353` 已实测为 `@@read_only=0` 写端点，`63350` 保持只读。代码必须固定读写端点与 `ads_ai.ad_control_action_log`，运行时禁用DDL，单写并发1、突发2/平均1QPS、JSON上限512KiB、连接3秒/IO5秒，失败零重试并保留SQLite outbox；API与runner的live preview跨账户并发统一默认4。
- 旧 `ad_control_rule` 路径未改造；生产已确认列表为空，若未来重新启用需先接入本日志链路。

## 变更记录

| 日期 | 内容 |
| --- | --- |
| 2026-07-15 | 初版：执行效率、限流续跑、ads_ai 审计表和日志 UI 优化。 |
| 2026-07-15 | 二次优化：业务日汇总、partial 状态拆分、历史迁移状态兼容与批次审计展开。 |
