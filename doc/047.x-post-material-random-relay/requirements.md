# 047.x-post-material-random-relay 需求与技术设计

## 背景

普通自动素材池原合同会优先把长素材分配给 Premium 目标账号；非会员目标账号不会通过会员账号中继。业务要求素材与目标个号在同语言范围内随机分配：当随机目标个号没有会员且素材时长超过 140 秒时，由随机合格会员账号先发原帖，再由目标个号 Repost。

## 目标

- 保留素材候选来源的 `created_at DESC,id DESC` FIFO 边界。
- 在每个冻结的自动素材 schedule slot 内，稳定随机分配同语言素材与目标账号。
- `>140s` 且目标非会员时，稳定随机选择同语言、active、approved、public、Token 当前确认的 Premium 源账号，执行 Post -> Repost。
- 计划创建后以 queue 为不可变审计事实；重启或重复轮询不得重抽。

## 范围

### 包含

- `source_type=material` 的正式 schedule 自动发布。
- runner 随机配对、OAuth 当前资格复核、store 原子冻结、relay/repost 状态机、素材池成功状态。
- SQLite 触发器允许 material schedule long relay，并继续阻止短素材 relay、同账号 relay、非 schedule material relay。

### 不包含

- operator manual、auto-template/X Auto、canary、短剧池分配策略。
- legacy daily/catch-up 的 relay 扩面。
- 真实 X Post/Repost、run-now、生产部署。

## 用户故事 / 业务规则

1. 素材候选集合仍按池记录 FIFO 倒序扫描；随机只发生在同语言候选与同语言目标账号之间。
2. 随机种子为版本化 slot 身份：workflow version、source type、run date、publish time、config version、canonical language、冻结账号 ID 列表；禁止使用 Python `hash()`。
3. `duration <= 140.0`：目标账号直接发布。
4. `duration > 140.0` 且目标当前具备 Premium 长视频资格：目标账号直接发布。
5. `duration > 140.0` 且目标不具备资格：随机选择同语言合格 relay；queue 的 `account_id` 始终是目标个号，`delivery_mode=premium_relay_repost`，并冻结 `relay_account_id`。
6. 随机目标需要 relay 但没有同语言合格 relay 时，立即整批 `failed_preflight`；不得用更旧短素材绕过最新 FIFO 长素材。长素材保持未绑定、可在未来资格变化后重试，且零 queue、零 binding、零 X 写入。
7. relay 原帖确认后素材池仍为 `unpublished`；只有目标 Repost 确认后才改为 `published`。
8. source/repost unknown 均保留 `needs_review`，不得自动重试；source attempt 开始后禁止换 relay，零 attempt 才可重选。
9. drama relay 继续按历史 least-load；manual 与 X Auto 合同不变。

## 交互与流程

`FIFO candidates -> canonical-language buckets -> stable target shuffle -> media preflight -> direct or stable relay shuffle -> OAuth reverify -> one transaction(run + queues + pool bindings + repost ledger) -> relay Post -> target Repost -> pool published`

## 技术设计

### 影响模块

- `scripts/x_post_schedule_runner.py`
- `features/x_accounts/oauth_service.py`
- `features/x_posts/service.py`
- `scripts/test_x_post_material_random_relay.py`
- `scripts/test_x_post_multi_schedule_store.py`

### 数据结构

不新增列。稳定随机结果最终冻结在现有 `x_post_queue.account_id/delivery_mode/relay_account_id` 与 `x_post_repost_ledger`。迁移只重建幂等 SQLite triggers，不重写历史行。

### API / 接口

沿用内部接口：

- `POST /internal/posts/premium-relay/accounts`
- `POST /internal/posts/schedule-plan`
- `POST /internal/posts/queue/{queue_id}/publish`

候选 payload 对 material long relay 使用既有 `delivery_mode`、`relay_account_id`、`relay_account_username` 字段。

### 异常与边界

- `140.0s` 直接；`140.001s` 进入 Premium 能力判断。
- relay 必须与 target/content 同语言、不同账号、公开且当前 Token 确认可发布长视频。
- OAuth 复核发现冻结 relay 已失效时整批计划创建失败；不得悄悄换成列表第一项。
- 多语言 relay 列表按 queue 语言过滤，禁止展平后跨语言。

## 验收标准

- 稳定 seed 可注入测试，重复执行结果一致。
- 同语言随机配对、两个时长边界、多 relay 选择、无 relay 整批失败全部通过。
- 计划重放保持 queue/material/target/relay 不变。
- 无 relay 或跨语言 relay 时整批零 queue/零 binding。
- relay source 成功不改 pool，target Repost 成功才改 pool。
- manual/X Auto/短 relay/cross-language/同账号继续失败闭合。
- drama least-load 与 unknown/retry fences 回归通过。
- 全部 `scripts/test_x_*.py`、`py_compile`、`git diff --check` 通过。

## 风险与待确认

- relay 会员状态随时变化，计划创建与最终 X 写入前均需按现有 Sidecar 再验证。
- 本次不部署；生产发布需 GitHub-first、SQLite online backup、不可变 release、自然 timer 验证，禁止真实 Post canary。

## 变更记录

- 2026-08-17：确认普通自动素材池随机配对及非会员长视频 Premium relay 合同，取代旧的“长素材只能直接分配给 Premium 目标账号”规则；素材来源 FIFO 保持不变。
