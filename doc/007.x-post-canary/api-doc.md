# API 文档

## 接口列表

| 方法 | 路径 | 可见性 | 用途 |
| --- | --- | --- | --- |
| POST | `/internal/posts/canary` | 仅 loopback + internal bearer | 人工触发一条经过审计的 X 灰度 Post |

## 请求/响应

请求体只接收非敏感业务字段：

```json
{
  "account_id": 2,
  "source_date": "2026-07-22",
  "material_id": "123",
  "content_id": "456",
  "material_url": "https://.../video.mp4",
  "material_name": "material-name",
  "material_language": "English",
  "drama_name": "Drama name",
  "tag": "Drama",
  "description": "Drama description"
}
```

`queue_id`、`log_id`、X 用户名、page 名与 page ID 均由服务端生成或从已校验账号覆盖，调用方不能指定。

成功响应：

```json
{
  "item": {
    "status": "published",
    "log_id": 1,
    "short_url": "https://ai.yingliangads.com/s2l/1.html",
    "post_id": "1234567890",
    "preview_url": "https://x.com/example/status/1234567890"
  }
}
```

响应永不包含 Access Token、Refresh Token、内部 bearer 或完整素材鉴权信息。

## 错误码

| 错误码 | HTTP | 含义 |
| --- | --- | --- |
| `invalid_request` | 400/413 | 字段、URL、正文或请求大小不合法 |
| `x_internal_auth_failed` | 403 | 非 loopback 或 internal bearer 错误 |
| `x_account_not_found` | 404 | 账号不存在 |
| `x_account_disabled` | 409 | 账号已停用 |
| `x_account_not_publishable` | 409 | 账号状态/scope/Token 不满足发布条件 |
| `x_post_duplicate` | 409 | 幂等键已处于发布中、成功或未知状态 |
| `x_media_invalid` | 422 | 素材下载或视频预检失败 |
| `x_upstream_error` | 502 | X 明确返回失败 |
| `x_publish_unknown` | 503 | Create Post 已发送但结果不确定，禁止自动重试 |

## 兼容性说明

接口为内部运维 canary，不接入公网 Nginx。原 `/internal/accounts/*` 以及 OAuth 公网接口保持兼容；新增 SQLite 表为向后兼容迁移。
