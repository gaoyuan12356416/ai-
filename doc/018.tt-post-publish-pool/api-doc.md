# API 文档

## 接口列表

| 方法 | 路径 | 说明 | 权限 |
| --- | --- | --- | --- |
| GET | `/api/admin/tt-posts/accounts` | 安全账号列表和数据库候选状态 | `ttPostPool` |
| POST | `/api/admin/tt-posts/creator-info` | GPU 实时账号能力预检 | `ttPostPool` + 同源 |
| POST | `/api/admin/tt-posts/materials/preview` | 素材、Drama ID和 GPU 成片预览 | `ttPostPool` + 同源 |
| GET | `/api/admin/tt-posts/queue` | 查询发布任务 | `ttPostPool` |
| POST | `/api/admin/tt-posts/queue` | 冻结并创建任务 | `ttPostPool` + 同源 |
| POST | `/api/admin/tt-posts/queue/{id}/cancel` | 取消 init 前任务 | `ttPostPool` + 同源 |
| POST | `/api/admin/tt-posts/queue/{id}/reconcile` | 人工核对 GPU 账本中的已有结果 | `ttPostPool` + 同源 |
| GET | `/api/admin/tt-posts/events?queue_id={id}` | 查询只追加事件 | `ttPostPool` |

内部 GPU 端点：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 不含密钥的健康状态 |
| POST | `/internal/tt-post/creator-info` | 调用 TikTok `creator_info` 并返回安全 DTO |
| POST | `/internal/tt-post/prepare` | 下载正片、拼片尾、动态 Drama ID、探测和固化 |
| POST | `/internal/tt-post/publish` | 三重门禁通过后执行 Direct Post init |
| POST | `/internal/tt-post/reconcile` | 按现有 `publish_id` 查询状态 |

## 请求/响应

创建任务：

```json
{
  "idempotency_key": "tt-post:019fa...",
  "material_id": "5824343",
  "content_id": "Y9v1yQcFqM",
  "source_account_id": "700",
  "scheduled_at": "2026-07-30T02:00:00.000Z",
  "timezone": "Asia/Shanghai",
  "caption_text": "Watch the full story in the app 🎬\n\nDrama ID: Y9v1yQcFqM\n\nVisit my profile → Open the link → Search the Drama ID → Watch now.",
  "privacy_level": "SELF_ONLY",
  "allow_comment": false,
  "allow_duet": false,
  "allow_stitch": false,
  "commercial_disclosure": false,
  "brand_organic_toggle": false,
  "brand_content_toggle": false,
  "is_aigc": false,
  "publish_mode": "hold",
  "consent": {
    "accepted": true,
    "version": "tt-direct-post-consent-20260729",
    "accepted_at": "2026-07-29T07:00:00.000Z"
  }
}
```

成功响应只返回安全字段：

```json
{
  "item": {
    "id": 1,
    "material_id": "5824343",
    "content_id": "Y9v1yQcFqM",
    "source_account_id": "700",
    "account_name_snapshot": "Dramawave Short Dramas",
    "scheduled_at": "2026-07-30T02:00:00Z",
    "status": "scheduled",
    "publish_mode": "hold"
  },
  "gates": {
    "live_enabled": false,
    "audit_approved": false,
    "url_property_verified": false,
    "is_open": false
  }
}
```

账号列表禁止包含 `access_token`、`refresh_token`、Authorization 或数据库密码。

GPU publish 请求中的敏感账号凭证只存在于 AES-GCM 短时任务信封内，服务端不记录请求头/请求体；响应只含 `publish_id`、远端状态、TikTok log ID和安全错误。

## 错误码

| HTTP | code | 含义 |
| --- | --- | --- |
| 400 | `invalid_request` | 字段或时间格式错误 |
| 400 | `tt_caption_content_id_missing` | 描述未包含真实 Drama ID |
| 400 | `tt_post_consent_required` | 未完成显式发布同意 |
| 403 | `permission_denied` | 无 TT 发布池权限 |
| 404 | `tt_account_not_found` | 账号不存在或不满足候选条件 |
| 409 | `tt_post_material_already_used` | 素材已有排期或发布历史 |
| 409 | `tt_post_account_schedule_conflict` | 同账号同一时间已有任务 |
| 409 | `tt_creator_info_changed` | 账号实时能力与冻结快照不同 |
| 409 | `tt_post_unknown_no_retry` | 结果不明，禁止自动重发 |
| 409 | `tt_post_reconcile_only` | 已有 `publish_id`，只能 reconcile |
| 502 | `tt_upstream_rejected` | TikTok 返回已脱敏错误 |
| 503 | `tt_post_service_unavailable` | CPU sidecar 或其依赖暂不可用 |

## 兼容性说明

- 用户原文变量为 `{{contect_id}}`，模板渲染兼容该拼写；数据库和 API 始终使用正确字段名 `content_id`。
- 时间输入为 `Asia/Shanghai`，数据库统一存 UTC。
- 本功能完全独立于 `x_post_*` 表和 X 发布状态。
- 三重门禁默认关闭；关闭态 API 仍支持账号、素材、成片、队列和对账演练，但不调用 TikTok Direct Post init。
