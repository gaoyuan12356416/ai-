# API 文档

## 认证与边界

- `/api/admin/tt-posts/*`：继续由 AI 后台登录态与主 API 代理保护。
- `/internal/tt-posts/preparations/*`：仅允许 CPU 本机 `127.0.0.1:18829`，使用既有独立 `TT_POST_INTERNAL_TOKEN`；不得经 Nginx 或公网暴露。
- 所有请求/响应为 JSON；公共响应不得包含 claim token、内部 bearer、lease 细节或完整敏感凭据。

## 接口列表

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/api/admin/tt-posts/materials/preview` | 快速校验素材，不调用 GPU |
| POST | `/api/admin/tt-posts/materials/prepare` | 兼容别名，语义同 preview |
| POST | `/api/admin/tt-posts/material-pool` | 将已校验素材持久化为 queued intake |
| GET | `/api/admin/tt-posts/material-pool` | 合并查询 intake 与 ready pool |
| POST | `/internal/tt-posts/preparations/claim` | runner 领取一条可处理 intake |
| POST | `/internal/tt-posts/preparations/{id}/renew` | 续租当前 claim |
| POST | `/internal/tt-posts/preparations/{id}/process` | 执行 GPU prepare 并持久化结果 |

## 请求与响应

### 1. 快速校验素材

请求：

```json
{
  "material_id": "5391678"
}
```

成功响应核心字段：

```json
{
  "item": {
    "material_id": "5391678",
    "content_id": "Ag0rfr5F0F",
    "source_media_url": "https://example.invalid/source.mp4",
    "status": "validated",
    "status_label": "素材校验通过，可加入素材池",
    "preparation_status": "not_started",
    "publish_ready": false
  },
  "gates": {
    "live_enabled": false,
    "direct_audit_approved": false,
    "url_property_verified": false
  }
}
```

合同：只调用只读素材 resolver；不下载视频、不调用 GPU、不返回成片 URL/时长。

### 2. 素材入池

请求：

```json
{
  "idempotency_key": "tt-post:pool:batch-uuid:5391678",
  "source_account_id": "123456789",
  "material_id": "5391678",
  "content_id": "Ag0rfr5F0F",
  "caption_template": "Watch the full story in the app 🎬\n\nDrama ID: {{contect_id}}\n\nVisit my profile → Open the link → Search the Drama ID → Watch now.",
  "consent": {
    "accepted": true,
    "version": "tt-post-pool-v1",
    "accepted_at": "2026-07-30T10:30:00Z"
  }
}
```

成功响应核心字段：

```json
{
  "item": {
    "id": 17,
    "material_id": "5391678",
    "source_account_id": "123456789",
    "content_id": "Ag0rfr5F0F",
    "preparation_status": "queued",
    "publish_ready": false,
    "attempt_count": 0,
    "pool_item_type": "intake"
  },
  "available_material_count": 0,
  "preparation_wakeup_requested": true,
  "preparation_timer_fallback_seconds": 60
}
```

合同：

- API 再次解析素材并核对真实 `content_id`。
- `caption_text` 由服务端按模板渲染并冻结。
- 同键同请求返回已有行；响应可能已是 `preparing/retry_wait/ready/failed`，调用方不得强制改回 queued。
- `preparation_wakeup_requested=false` 不代表入池失败，timer 会在最多约 60 秒后兜底。

### 3. 查询素材池

查询参数：

- `page`：从 1 开始。
- `page_size`：1–100。
- `source_account_id`：可选账号过滤。
- `material_id`：可选素材过滤。
- `status`：可匹配预制作状态或 ready pool 发布状态。

响应核心字段：

```json
{
  "items": [
    {
      "id": 17,
      "material_id": "5391678",
      "content_id": "Ag0rfr5F0F",
      "source_account_id": "123456789",
      "preparation_status": "preparing",
      "publish_ready": false,
      "attempt_count": 1,
      "next_attempt_at_utc": "",
      "error_code": "",
      "error_message": ""
    }
  ],
  "pagination": {
    "page": 1,
    "page_size": 20,
    "total": 1
  },
  "summary": {
    "total": 1,
    "queued": 0,
    "preparing": 1,
    "retry_wait": 0,
    "ready": 0,
    "failed": 0,
    "canceled": 0,
    "available": 0,
    "reserved": 0,
    "consumed": 0
  }
}
```

关联到 ready pool 的 intake 只返回一次；完成后 `preparation_status=ready`、`publish_ready=true`，并带 `pool_item_id`/ready pool 状态。

### 4. 内部领取

请求：

```json
{
  "worker_id": "tt-post-prepare-primary",
  "lease_seconds": 180
}
```

空闲响应：

```json
{
  "item": null
}
```

成功响应：

```json
{
  "item": {
    "id": 17,
    "material_id": "5391678",
    "preparation_status": "preparing",
    "attempt_count": 1
  },
  "claim_token": "<opaque-one-time-owner-token>"
}
```

claim 每次最多一条，遵守账号 FIFO；过期 `preparing` 可被新 token reclaim。

### 5. 内部续租

请求：

```json
{
  "claim_token": "<opaque-token>",
  "lease_seconds": 180
}
```

成功返回当前 intake；token 错误、状态不为 preparing 或 lease 已过期返回 `409`。

### 6. 内部执行

请求：

```json
{
  "claim_token": "<opaque-token>"
}
```

成功时：

```json
{
  "item": {
    "id": 17,
    "preparation_status": "ready",
    "publish_ready": true,
    "pool_item_id": 9,
    "prepared_duration_sec": 2091.33
  }
}
```

失败但状态已安全落库时仍返回业务结果，并带：

```json
{
  "item": {
    "id": 17,
    "preparation_status": "retry_wait",
    "publish_ready": false,
    "next_attempt_at_utc": "2026-07-30T10:35:30Z"
  },
  "processing_error": {
    "code": "prepare_timeout",
    "retryable": true
  }
}
```

## 主要错误码

| HTTP | code | 含义 |
| --- | --- | --- |
| 400 | `invalid_request` | 字段集合、格式或范围无效 |
| 404 | 素材 resolver 的 not-found code | 素材不存在/不符合可用条件 |
| 409 | `tt_content_id_mismatch` | 页面 Drama ID 与素材真实映射不一致 |
| 409 | `tt_post_material_intake_idempotency_conflict` | 同幂等键用于不同冻结请求 |
| 409 | `tt_post_material_intake_conflict` | 同素材已以不同冻结信息入池 |
| 409 | `tt_post_material_already_used` | 素材已在其他池/队列/历史中 |
| 409 | `tt_post_material_intake_claim_invalid` | token 错误、状态变化或 lease 过期 |
| 409 | `tt_post_material_intake_artifact_mismatch` | 成片与冻结 job/profile/trim 不一致 |
| 409 | `tt_post_material_intake_completion_conflict` | ready 完成重放的成片身份不同 |
| 409 | `tt_prepared_media_duration_invalid` | 成片不满足账号实时视频时长限制 |
| 5xx | GPU/sidecar error code | 远端制作临时或系统错误，按策略 retry_wait/failed |

## 兼容性说明

- `/materials/prepare` 路径保留，但不再承诺同步返回成片。
- `GET /material-pool` 继续返回既有 ready pool 字段，并新增 `preparation_status`、`publish_ready`、`pool_item_type`。
- 既有 schedule/run-now/publish API 不变；它们只消费 `tt_post_recurring_pool(status=available)`。
- 客户端必须以 `publish_ready` 判断可发布性，不能把“入池成功”视为“成片完成”。
