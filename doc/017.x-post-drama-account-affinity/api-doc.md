# API 文档

## 接口列表

- `POST /internal/posts/drama-pool/available`：按冻结账号顺序返回每个账号唯一候选短剧。
- `POST /internal/posts/schedule-plan`：事务内校验归属并创建定时队列。
- `POST /internal/posts/queue/{queue_id}/publish`：发布前再次校验短剧归属。
- `POST /internal/posts/drama-pool/query`：返回短剧池及绑定账号展示字段。

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

## 错误码

| 错误码 | HTTP | 含义 |
| --- | --- | --- |
| `x_post_drama_owner_not_configured` | 409 | 启用配置缺少未完结归属账号 |
| `x_post_schedule_drama_shortage` | 409 | 候选短剧不足，整批不建队列 |
| `x_post_drama_assignment_conflict` | 409 | 客户端候选与事务内映射不一致 |
| `x_post_drama_account_binding_conflict` | 409 | 队列账号与短剧归属不一致 |
| `x_post_drama_pool_needs_review` | 409 | 存在待人工确认短剧 |
| `x_post_storage_conflict` | 500 | 数据库归属证据或唯一性冲突 |

## 兼容性说明

- 新字段为 SQLite 加法迁移，旧历史队列和日志不删除。
- `account_ids` 是新版短剧 runner 的必填内部契约。
- 旧版 runner 不具备粘性分配能力，数据库升级后不得与新版 schema 混用运行。
