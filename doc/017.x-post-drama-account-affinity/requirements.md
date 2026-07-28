# 017.x-post-drama-account-affinity 需求与技术设计

## 背景

旧选择器会把入池最早短剧的连续免费集展开到“账号数”，再与账号顺序配对，导致同一短剧由多个 X 账号交叉发布。生产短剧 `bURak9Oyn7` 的 Episode 1–7 已出现此历史行为。

## 目标

- 一部未完结短剧固定由一个 X 账号续发。
- 一个 X 账号同一时刻只负责一部未完结短剧。
- 没有在播短剧的账号领取最早加入且从未被分配的新剧。
- 选择、建队列、发布和历史迁移都由服务端强制校验，不能只依赖页面或 runner。

## 范围

### 包含

- 短剧池账号归属字段、迁移、唯一约束和触发器。
- sidecar 按冻结账号顺序返回账号—短剧候选。
- 选择器每个账号只取归属短剧的下一免费集。
- 建计划事务内重新计算并校验 `(account_id, pool_id, episode_number)`。
- 发布前阻断与归属不一致的旧冻结队列。
- 页面显示持久化“绑定账号”。

### 不包含

- 不删除、不改写已发布 Post、发布日志或历史队列。
- 不自动把失败、结果未知或待人工确认的短剧换给其他账号。
- 不增加页面手工换绑功能。
- 本需求上线时不主动执行额外发布。

## 用户故事 / 业务规则

1. 首次分配按当前配置的账号顺序，将最早入池的未绑定短剧一一绑定。
2. 已绑定账号在每个发布时间点只领取该剧当前 `next_sub_number`。
3. 调整账号顺序不改变已有绑定。
4. 新增账号领取最早的未绑定短剧。
5. 免费集全部发布成功后，账号在下一个发布时间点才可领取下一部未绑定短剧。
6. 已绑定短剧发生失败、结果未知或 `needs_review` 时保留绑定并停止自动推进。
7. 自动发布仍启用时，移除有未完结绑定的账号返回 409；关闭整个短剧定时任务可以暂停并保留绑定。
8. 短剧数量不足以覆盖全部账号时整批预检失败，不创建部分队列。
9. 账号、短剧和集数的最终映射以建计划事务内的重新计算结果为准。

## 历史迁移规则

- 无历史队列：保持未绑定。
- 有历史队列：优先选择“队列与日志均为已发布且结果明确”的最早集账号；没有确认发布记录时选择最早预留队列账号。
- 已存在非零绑定且与迁移结果冲突：中止迁移，不覆盖。
- 一个账号若迁移后同时占用多部未完结短剧：唯一索引使迁移失败并回滚。
- 保留所有旧队列和日志。生产 `bURak9Oyn7` 的确定归属为最早确认发布 Episode 1 的账号 ID 10；Episode 8–11 只能由账号 10 继续发布。

## 交互与流程

1. runner 使用冻结计划中的 `account_ids` 请求可用短剧。
2. sidecar 在 SQLite 快照中先取每个账号未完结绑定，再按账号顺序补充未绑定 FIFO 短剧。
3. 选择器只读 `drama_resource`，校验完整免费集并返回每剧下一集。
4. 媒体预检完成后，sidecar 使用 `BEGIN IMMEDIATE` 重新计算映射。
5. 新剧在插入首条队列后，同事务写入账号、绑定时间和绑定依据队列 ID。
6. 发布前再次断言队列账号等于短剧归属账号。
7. 发布成功后推进 `next_sub_number`；完成时释放账号的“未完结唯一占用”。

## 技术设计

### 影响模块

- `features/x_posts/service.py`
- `features/x_posts/drama_selector.py`
- `features/x_accounts/oauth_service.py`
- `scripts/x_post_schedule_runner.py`
- `static/x-post-drama-pool.html`

### 数据结构

`x_post_drama_pool` 新增：

- `assigned_account_id INTEGER NOT NULL DEFAULT 0`
- `assigned_at TEXT NOT NULL DEFAULT ''`
- `assigned_source_queue_id INTEGER`

约束：

- 未完结短剧按 `assigned_account_id` 建部分唯一索引。
- 非零绑定账号、绑定时间和绑定依据队列不可修改或清空，依据队列不可删除。
- 绑定必须有同剧、同账号的依据队列。
- drama 队列插入或关键身份字段更新时，账号必须等于归属账号；首次队列允许池记录尚未绑定。

### API / 接口

`POST /internal/posts/drama-pool/available` 增加 `account_ids`，响应每项增加 `assigned_account_id`、`assigned_at`、`assigned_source_queue_id` 和 `candidate_account_id`。

短剧池查询响应增加 `assigned_account_username`，页面不再使用 `last_account_*` 作为当前归属。

### 异常与边界

- `x_post_drama_owner_not_configured`：启用配置缺少未完结短剧归属账号。
- `x_post_schedule_drama_shortage`：未完结绑定加未绑定短剧不足以覆盖全部账号。
- `x_post_drama_assignment_conflict`：候选与事务内归属或 FIFO 结果不一致。
- `x_post_drama_account_binding_conflict`：发布队列账号与短剧归属不一致。
- `x_post_drama_pool_needs_review`：存在待人工确认短剧，保持原有全局暂停语义。

页面展示按状态区分：未绑定可用剧为“待分配”，校验失败为“不可分配”，已完成剧的保留归属为“历史发布账号”，待核查绑定显示为暂停。

## 验收标准

- A、B 两账号与 D1、D2 两剧：首次 A→D1E1、B→D2E1；下一点 A→D1E2、B→D2E2。
- 将账号顺序改成 B、A 后，仍为 B→D2、A→D1。
- 新增 C 后，C 获得最早未绑定剧，A/B 不换剧。
- D1 完成后，A 下一点获得最早未绑定剧。
- 跨账号续发被服务、触发器和发布入口阻断。
- 历史混发记录不被改写，迁移后只有一个确定归属。
- 短剧不足时零队列落库。
- 页面绑定账号与数据库归属一致。

## 风险与待确认

- 生产数据库升级前必须做 SQLite 在线备份和副本演练。
- 不允许在存在 `claimed/queued/running/publishing/unknown` 的短剧批次时升级。
- 旧版本 runner 不理解账号归属；代码回滚时必须先暂停短剧定时任务，不能让旧代码继续运行。

## 变更记录

- 2026-07-28：确认粘性账号规则、历史确定性迁移规则和生产预期映射。
