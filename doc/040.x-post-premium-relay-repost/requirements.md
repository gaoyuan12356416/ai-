# 040.x-post-premium-relay-repost 需求与技术设计

## 背景与目标

短剧固定绑定的目标账号可能没有 X Premium。当前 `>140s` 视频会在媒体预检阶段返回 `x_long_video_requires_premium`；历史逻辑还会把已绑定短剧置为 `needs_review`，阻塞整个短剧池。

目标：保持短剧与目标账号的固定归属；长视频由当前授权账号中具备 token 确认会员资格的公开账号发布原 Post，再由目标账号通过 X 官方 Repost API 转发。会员账号增加后自动均衡分摊。

## 范围

### 包含

- 当前授权、已批准、状态 active、`subscription_type in (basic,premium,premium_plus)`、`protected=false` 的账号作为中转源候选。
- `>140s + 目标非会员` 使用中转；`<=140s` 及目标本身有会员保持原直发逻辑。
- 按历史累计已冻结中转任务数最少选择，平局按账号 ID 稳定排序；事务内分配，不在午夜清零，避免每天仅一条任务时总落到同一账号。新增会员会优先承接任务直至累计负载追平。
- 原 Post、目标 Repost 的独立持久化状态、去重、未知结果关闸及旧确定性零写入阻断恢复。
- X OAuth 现有 `tweet.read tweet.write users.read` 权限下调用 `POST /2/users/{id}/retweets`。

### 不包含

- 不改变短剧目标账号绑定关系。
- 不对生产发真实 Post/Repost，不部署、不重启服务。
- 不把 Repost 描述为目标账号原创；原帖归属和展示遵循 X 平台语义。

## 业务规则

1. 每次计划前实时刷新可用会员账号；每次原帖前在账号锁内再次确认会员资格与公开状态。
2. 中转源在原帖尚未开始前失效，可按最新负载安全重选；原帖开始后绝不换源。
3. 原帖确认后状态为 `source_published`，此时不得推进短剧集数，也不得再次上传/创建原帖。
4. 只有目标 Repost 明确返回 `retweeted=true` 并完成本地事务后，队列才 `published`，剧集才递增一次。
5. 原帖或 Repost 的网络未知结果进入 `needs_review`，后续自动流程不得盲重试。
6. 没有会员账号时记录 `x_post_premium_relay_unavailable`，短剧仍保持可重试，不污染整个池。
7. 迁移只自动恢复 `needs_review + x_long_video_requires_premium + 当前集无队列证据` 的历史记录，并写追加审计；任何有当前集队列/写入证据的记录不动。

## 数据结构

- `x_post_queue` 加法列：`delivery_mode`、`relay_account_id`、`relay_account_username`。
- 新表 `x_post_repost_ledger`：冻结目标、会员源、原 Post ID/URL、Repost ID、双阶段尝试次数、状态、未知结果和错误证据。
- 新表 `x_post_drama_capability_block_recovery`：历史确定性零写入阻断恢复审计。
- 唯一约束：每队列一个 Repost ledger；`(source_post_id,target_account_id)` 非空时唯一。

## 状态流

`reserved -> source_publishing -> source_published -> reposting -> reposted`

- 原帖未知或转发未知：`needs_review`。
- 明确失败：`failed`。
- `source_published` 是唯一允许继续目标 Repost、禁止重新发原帖的可恢复状态。

## 验收标准

1. 单会员时所有中转任务由该账号承接；多会员按累计任务量均衡，连续分配后的负载差不超过 1，跨天和重启后顺序不漂移。
2. 非会员、未批准、非 active、资格 unknown、受保护账号均不得成为中转源。
3. 目标会员直发和所有短视频路径保持不变。
4. 原帖确认但 Repost 失败/未知时，原帖上传/创建调用始终只有一次。
5. Repost 成功才推进短剧；重复回放不会重复推进。
6. 全量 `test_x_*.py`、专项测试、`py_compile`、`git diff --check` 全部通过。

## 风险

- X Repost 会保留原帖归属，不等同目标账号原创。
- 首次生产能力证明仍需自然调度或明确授权的小流量验证；本地测试只验证协议和状态机，不声称完成真实平台写入。

## 变更记录

- 2026-08-12：根据用户确认，新增“识别全部绑定会员账号并平均分摊”的方案与实现。
