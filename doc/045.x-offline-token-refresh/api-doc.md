# API 文档

## 接口列表

- `GET /api/x-accounts`
- `GET /api/admin/x-accounts`
- `POST /internal/posts/accounts/{id}/verify`
- `POST /internal/posts/auto-template/accounts/{id}/verify`

## 请求/响应

账号查询响应在现有 item 上新增：

```json
{
  "status": "active",
  "access_token_expired": true,
  "refresh_token_available": true,
  "authorization_refreshable": true,
  "access_token_status": "expired_refreshable"
}
```

内部按需刷新请求使用：

```json
{
  "only_refresh_required": true,
  "preserve_transient_status": true,
  "require_publish_approved": true
}
```

## 错误码

- `x_account_publish_not_approved`：账号未允许发布。
- `x_token_revoked`：Refresh Token 缺失、撤销或 `invalid_grant`，需重新授权。
- `x_post_rate_limited` / `x_upstream_error`：本次安全失败，自动路径不误标撤销。
- `x_identity_mismatch`：刷新后的 Token 不属于冻结账号，禁止发布。

## 兼容性说明

只新增响应字段和内部请求参数；旧调用继续可用。任何接口、DOM、日志均不返回 Token 内容。
