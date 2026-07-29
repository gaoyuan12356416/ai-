# API 文档

## 接口列表

- `POST /internal/posts/drama-pool/available`：按冻结账号顺序返回每个账号唯一候选短剧。
- `POST /internal/posts/schedule-plan`：事务内校验归属并创建定时队列。
- `POST /internal/posts/queue/{queue_id}/publish`：发布前再次校验短剧归属。
- `POST /internal/posts/drama-pool/query`：返回短剧池及绑定账号展示字段。
- `POST /internal/posts/drama-pool/check`：记录未绑定、无历史新剧的确定性校验失败。

## 请求/响应

### 可用短剧

请求：

```json
{
  "limit": 100,
  "account_ids": [10, 9, 8]
}
```

响应：

```json
{
  "items": [
    {
      "id": 2,
      "content_id": "bURak9Oyn7",
      "next_sub_number": 8,
      "assigned_account_id": 10,
      "assigned_at": "2026-07-28T03:15:00Z",
      "assigned_source_queue_id": 35,
      "candidate_account_id": 10
    }
  ]
}
```

规则：

- `account_ids` 必须为 1–50 个不重复正整数。
- 响应项最多等于账号数，按请求账号顺序排列。
- 已绑定项的 `assigned_account_id` 必须等于 `candidate_account_id`。
- 未绑定项只在建计划事务成功时正式写入归属。

### 短剧池查询

响应项新增：

```json
{
  "assigned_account_id": 10,
  "assigned_account_username": "SecretAffa6ann",
  "assigned_at": "2026-07-28T03:15:00Z",
  "assigned_source_queue_id": 35
}
```

### 记录未绑定新剧校验失败

`POST /internal/posts/drama-pool/check`

请求：

```json
{
  "checks": [
    {
      "pool_item_id": 53,
      "content_id": "DRAMA-BAD",
      "error_code": "source_not_repairable",
      "error_message": "episode media preflight failed"
    }
  ]
}
```

成功响应：

```json
{
  "item": {
    "updated_count": 1
  }
}
```

安全约束：

- `checks` 必须包含 1–100 个对象，`pool_item_id` 不得重复，且 ID 与 `content_id` 必须精确匹配数据库记录。
- 仅允许将“未绑定且没有任何 drama 队列/发布历史”的未完成短剧写为 `validation_failed`，同时保存脱敏错误。
- 已绑定短剧或已有任何队列/发布历史的短剧返回 `x_post_drama_pool_item_bound`；调用方必须走批次失败记录，使其进入 `needs_review` 并停止整批，不能用本接口跳过或换绑。
- 任一项校验不通过时整个请求事务回滚；该接口不建队列、不绑定账号、不触发发布。

## 错误码

| 错误码 | HTTP | 含义 |
| --- | --- | --- |
| `x_post_drama_owner_not_configured` | 409 | 启用配置缺少未完结归属账号 |
| `x_post_schedule_drama_shortage` | 409 | 候选短剧不足，整批不建队列 |
| `x_post_drama_assignment_conflict` | 409 | 客户端候选与事务内映射不一致 |
| `x_post_drama_account_binding_conflict` | 409 | 队列账号与短剧归属不一致 |
| `x_post_drama_pool_needs_review` | 409 | 存在待人工确认短剧 |
| `x_post_drama_pool_item_unavailable` | 409 | 校验记录与短剧池身份不一致，或短剧已完成 |
| `x_post_drama_pool_item_bound` | 409 | 短剧已绑定或有队列/发布历史，不能跳过 |
| `x_post_storage_conflict` | 500 | 数据库归属证据或唯一性冲突 |

## 兼容性说明

- 新字段为 SQLite 加法迁移，旧历史队列和日志不删除。
- `account_ids` 是新版短剧 runner 的必填内部契约。
- `drama-pool/check` 仅对受信任内部调用方开放，并复用现有内部鉴权；不得暴露为页面匿名能力。
- `drama-pool/check` 的 FIFO 顺延由 runner 在成功写入 `validation_failed` 后重新请求候选完成。
- 本需求不提供失败批次恢复或补发 API。10:06 不补跑；若需补发必须另立包含管理员审批、不可变审计和停用配置门禁的需求。
- 旧版 runner 不具备粘性分配能力，数据库升级后不得与新版 schema 混用运行。
