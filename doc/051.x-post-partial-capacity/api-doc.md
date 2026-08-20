# API 文档

## 接口列表

- `POST /internal/posts/schedule-plan`
- `POST /internal/posts/schedule-plan/query`
- `POST /internal/posts/schedules/due`

均为本机 Bearer 保护的内部接口，外部管理 API 无结构变化。

## 请求/响应

`schedule-plan` 的 `account_ids` 与 `candidates` 数量相等，且必须是冻结配置账号的非空有序子集。`schedule-plan/query` 中 `run.account_ids` 返回完整冻结范围，`run.expected_count` 和 `queues` 返回实际计划数量。

示例语义：配置 `[11,12,13]`、实际候选 `[11,13]` 时，运行范围仍为 `[11,12,13]`，`expected_count=2`，队列账号为 `[11,13]`。

## 错误码

- `x_post_schedule_candidate_shortage`：候选为空或超过配置范围。
- `x_post_schedule_account_mismatch`：候选账号不是配置范围的有序子集。
- `x_post_schedule_material_preflight_shortage`：素材零条通过预检。
- `x_post_schedule_drama_shortage`：短剧零条可用或通过预检。
- `x_post_pool_fifo_conflict`：存在未被本轮有效证据覆盖的较新池记录，禁止越过建队。
- 既有去重、归属、Premium、未知结果错误码保持不变。

## 兼容性说明

完整批次行为不变。调用方如果仍提交完整账号列表会得到原有结果；新版本额外接受有序子集。无 DDL、无外部请求字段变化。

素材池 available 接口仍一次返回最多 `scan_limit` 条固定 FIFO 快照；runner 内部分页不改变接口结构。
