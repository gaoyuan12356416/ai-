# X账号授权管理 API

## 公共约定

- 所有 `/api/x-accounts*` 接口要求 Feishu Cookie及 `x_accounts` 模块权限，不接受普通 API Token。
- POST 必须使用 `Content-Type: application/json`；存在 `Origin`/`Referer` 时必须与请求 Host同源。
- Nginx与页面均设置 `Cache-Control: no-store` / `cache: no-store`。
- 所有时间字段统一为 ISO 8601 UTC，格式如 `2026-07-14T03:20:00Z`；空时间返回空字符串。
- 响应绝不包含 Client Secret、Access Token、Refresh Token、authorization code或 PKCE verifier；`authorization_url` 按 OAuth规范包含公开 Client ID与一次性 state。

## GET /api/x-accounts/config

```json
{
  "configured": true,
  "callback_url": "https://ai.yingliangads.com/x-oauth/callback",
  "scopes": ["tweet.read", "tweet.write", "users.read", "offline.access", "media.write"],
  "required_scopes": ["tweet.read", "tweet.write", "users.read", "offline.access", "media.write"],
  "state_ttl_seconds": 600
}
```

## GET /api/x-accounts

```json
{
  "items": [
    {
      "id": 1,
      "x_user_id": "123456789",
      "username": "example",
      "display_name": "Example",
      "profile_image_url": "https://...",
      "status": "active",
      "scopes": ["tweet.read", "tweet.write", "users.read", "offline.access", "media.write"],
      "missing_scopes": [],
      "first_authorized_at": "2026-07-14T03:20:00Z",
      "last_authorized_at": "2026-07-14T03:20:00Z",
      "access_expires_at": "2026-07-14T05:20:00Z",
      "last_token_refresh_at": "",
      "last_verified_at": "2026-07-14T03:20:00Z",
      "last_error_at": "",
      "last_error": "",
      "authorized_by_user_id": "ou_xxx",
      "authorized_by_name": "后台用户",
      "authorized_by_email": "user@example.com",
      "created_at": "2026-07-14T03:20:00Z",
      "updated_at": "2026-07-14T03:20:00Z"
    }
  ],
  "total": 1,
  "updated_at": "2026-07-14T03:21:00Z"
}
```

状态包括：`active`、`refresh_required`、`scope_missing`、`token_missing`、`revoked`、`error`。

## POST /api/x-accounts/authorize

请求体：`{}`。成功响应：

```json
{
  "authorization_url": "https://x.com/i/oauth2/authorize?...",
  "callback_url": "https://ai.yingliangads.com/x-oauth/callback",
  "scopes": ["tweet.read", "tweet.write", "users.read", "offline.access", "media.write"],
  "expires_at": "2026-07-14T03:30:00Z"
}
```

## POST /api/x-accounts/{id}/verify

请求体：`{}`。必要时使用 Refresh Token续期并原子保存轮换后的 Refresh Token，再调用 X `/2/users/me`。成功返回：

```json
{"item": {"id": 1, "x_user_id": "123456789", "status": "active"}}
```

同一账号的校验/刷新在 sidecar 内串行执行，防止 Refresh Token轮换竞态。

## 错误码

| HTTP | error | 含义 |
| --- | --- | --- |
| 400 | `invalid_request` | 请求参数或 OAuth state无效 |
| 401 | `unauthorized` | 未登录 AI 后台 |
| 403 | `permission_denied` / `cookie_auth_required` / `same_origin_required` | 权限或请求来源不允许 |
| 404 | `x_account_not_found` | 账号记录不存在 |
| 409 | `x_token_missing` / `x_token_revoked` / `x_identity_mismatch` | Token缺失、授权失效或 Token属主不匹配 |
| 502 | `x_upstream_error` | X API请求失败 |
| 503 | `x_oauth_not_configured` / `x_accounts_unavailable` | OAuth或 sidecar配置/运行异常 |

浏览器只接收白名单错误码和固定脱敏文案，不透传上游响应正文。

## 公共回调

`GET /x-oauth/callback?code=...&state=...` 由 sidecar 处理。成功后 302 到：

```text
/x-accounts.html?oauth=success
```

失败后 302 到：

```text
/x-accounts.html?oauth=error&reason=<safe_error_code>
```

callback query不写入 Nginx access log或 sidecar日志。授权开始、完成、失败仅以脱敏事件写入 `x_oauth_event`，不保存 URL、code、state或 Token。

## 内部 API

仅 `127.0.0.1:8810` 可达，并要求 `Authorization: Bearer <internal-token>`：

- `GET /internal/config`
- `GET /internal/accounts`
- `POST /internal/authorize`
- `POST /internal/accounts/{id}/verify`

Nginx仅公开 sidecar `/health` 与 `/callback`，其余 `/x-oauth/*` 返回 404。
