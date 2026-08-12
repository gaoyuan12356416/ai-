# 042.x-post-manual-scheduled-publish API

## POST /api/admin/x-posts/material-pool/manual-publish

Cookie 管理员、`xPostMaterialPool` 导航权限、same-origin JSON。

新请求：

```json
{
  "material_ids": ["123456"],
  "account_ids": [7],
  "idempotency_key": "x-post-manual-ui-<uuid>",
  "publish_mode": "scheduled",
  "scheduled_at": "2026-08-12T18:30:00+08:00"
}
```

兼容请求：仅包含 `material_ids`, `account_ids`, `idempotency_key` 时按 `immediate` 处理。

约束：

- `publish_mode`: `immediate|scheduled`，省略为 immediate。
- immediate 的 `scheduled_at` 必须省略或为空。
- scheduled 的 `scheduled_at` 必须为带时区、分钟精度、严格未来时间；UI 固定使用 `+08:00`。
- 素材和账号各 1-50 个、数量相同、各自不重复。
- 相同幂等键只能对应完全相同的素材、账号、方式、时间与操作者。
- operator manual 可显式复用既有 pool/历史 queue 素材；不会改写或重试旧队列。已有 active reservation 仍会拒绝本次创建。

成功返回 HTTP 202：

```json
{
  "item": {
    "id": 12,
    "publish_mode": "scheduled",
    "scheduled_at": "2026-08-12T10:30:00Z",
    "scheduled_timezone": "Asia/Shanghai",
    "status": "queued",
    "account_ids": [7],
    "material_ids": ["123456"],
    "expected_count": 1,
    "queues": []
  },
  "audit_recorded": true
}
```

## GET /api/admin/x-posts/material-pool/manual-runs/{id}

返回同一安全 DTO。`queued + publish_mode=scheduled + scheduled_at>now` 表示等待定时时间，reservation 记录不公开。

## Internal routes

- `POST /internal/posts/manual-runs/create`：透传 `publish_mode` / `scheduled_at`。
- `POST /internal/posts/manual-runs/claim`：未到期 scheduled 不返回；到期或 running 才返回。
- 现有 query/plan/failure 路由不变。

## 错误

| HTTP | code | 含义 |
| --- | --- | --- |
| 400 | `invalid_request` | 发布方式或定时时间格式/边界无效 |
| 409 | `x_post_idempotency_conflict` | 幂等键已对应其他参数 |
| 409 | `x_post_manual_material_unavailable` | 素材已被另一个 active manual/auto-template reservation 占用 |
| 409 | `x_account_not_publishable` | 账号创建时已不可发布 |
