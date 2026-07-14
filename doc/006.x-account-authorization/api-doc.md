# X账号授权管理 API

## 页面与角色

| 页面 | 用途 | 权限 |
| --- | --- | --- |
| `/x-accounts.html` | 当前用户个人 X 授权管理：多账号授权、校验、退出授权 | Feishu Cookie + `x_accounts` |
| `/x-account-list.html` | 所有 X 账号、owner 和资料快照列表；管理员同步 | 导航与数据仅 admin；静态页面壳可 200，非 admin 只显示权限门 |

管理员进入个人页时仍按自己的 `tenant_key + user_id` 过滤，不因 admin 身份自动返回全量数据。

## 公共约定

- 所有接口仅接受 Feishu Cookie，不接受普通 API Token；`/api/admin/x-accounts*` 额外要求 admin。
- Owner 身份是 Cookie 会话中的 `tenant_key + user_id` 联合值。任一为空、只匹配 `user_id` 或只在前端过滤都不满足契约。
- POST 使用 `Content-Type: application/json`；存在 `Origin`/`Referer` 时必须与 Host 同源。
- `/api/x-accounts/config`、owner 列表/写操作、`/api/admin/x-accounts*` 都由主 API 返回 `Cache-Control: no-store`；Nginx 对 admin 精确/前缀路由再次设置 no-store，两个页面 fetch 均显式 `cache: "no-store"`。
- 时间统一 ISO 8601 UTC `...Z`；空时间返回空字符串。
- 响应绝不包含 Client Secret、Access Token、Refresh Token、authorization code、state 或 PKCE verifier。
- 个人按 ID 操作非本人记录统一返回 404，避免记录枚举；admin API 使用独立权限门。

## 账号对象

账号资料是最近一次 callback 或 verify 的本地快照，打开列表不会实时请求 X。

```json
{
  "id": 1,
  "x_user_id": "123456789",
  "username": "example",
  "display_name": "Example",
  "profile_url": "https://x.com/example",
  "profile_image_url": "https://pbs.twimg.com/profile_images/...",
  "location": "",
  "x_created_at": "2020-01-01T00:00:00Z",
  "protected": false,
  "verified": true,
  "followers_count": 1200,
  "following_count": 88,
  "tweet_count": 345,
  "listed_count": 7,
  "like_count": null,
  "media_count": null,
  "profile_synced_at": "2026-07-14T03:20:00Z",
  "last_profile_sync_at": "2026-07-14T03:20:00Z",
  "status": "active",
  "scopes": ["tweet.read", "tweet.write", "users.read", "offline.access", "media.write"],
  "missing_scopes": [],
  "owner_tenant_key": "tenant_xxx",
  "owner_user_id": "ou_xxx",
  "owner_name": "后台用户",
  "owner_email": "user@example.com",
  "owner": {
    "tenant_key": "tenant_xxx",
    "user_id": "ou_xxx",
    "name": "后台用户",
    "email": "user@example.com"
  },
  "first_authorized_at": "2026-07-14T03:20:00Z",
  "last_authorized_at": "2026-07-14T03:20:00Z",
  "access_expires_at": "2026-07-14T05:20:00Z",
  "last_token_refresh_at": "",
  "last_verified_at": "2026-07-14T03:20:00Z",
  "disconnected_at": "",
  "disconnected_by_tenant_key": "",
  "disconnected_by_user_id": "",
  "disconnected_by_name": "",
  "last_error_at": "",
  "last_error": "",
  "created_at": "2026-07-14T03:20:00Z",
  "updated_at": "2026-07-14T03:20:00Z"
}
```

`profile_url` 只由服务端对匹配 `[A-Za-z0-9_]{1,50}` 的 `username` 构造；超过 50 字符或包含其他字符时返回空链接。它不是 X `/users/me` 的用户网站字段。`profile_synced_at` 是存储字段，`last_profile_sync_at` 是兼容别名。

状态至少包括：`active`、`refresh_required`、`scope_missing`、`token_missing`、`revoked`、`error`、`revoke_pending`、`disconnected`。`revoke_pending` 和 `disconnected` 是持久化终止/过渡状态，列表投影不得因 live Token 文件仍存在而改回 active。

页面时间展示约定：个人页“刷新/校验”显示 `last_token_refresh_at`、`last_verified_at`，“更新/退出”显示 `updated_at`、`disconnected_at`；admin 页另将 `access_expires_at` 与刷新时间同列、`last_profile_sync_at` 与校验时间同列。

## GET /api/x-accounts/config

要求个人模块权限。响应沿用：

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

仅返回当前 Cookie 的 `owner_tenant_key + owner_user_id` 记录。不得因为当前用户是 admin 而扩大范围。

```json
{
  "items": [{"id": 1, "x_user_id": "123456789", "username": "example", "status": "active"}],
  "total": 1,
  "updated_at": "2026-07-14T03:21:00Z"
}
```

`total` 是本人记录数，不是全局数量。owner 为空的 legacy 记录不会出现在此接口。

## POST /api/x-accounts/authorize

请求体 `{}`。服务端从 Cookie 生成 actor，禁止客户端提交或覆盖 owner。若该 owner 任一账号处于 `revoke_pending`，返回 409 `x_disconnect_pending`，必须先重试完成退出授权，不能为该 owner 创建新的 OAuth state。成功响应：

```json
{
  "authorization_url": "https://x.com/i/oauth2/authorize?...",
  "callback_url": "https://ai.yingliangads.com/x-oauth/callback",
  "scopes": ["tweet.read", "tweet.write", "users.read", "offline.access", "media.write"],
  "expires_at": "2026-07-14T03:30:00Z"
}
```

Callback 恢复 state 中的 actor；owner 锁覆盖 pending precheck 与 token exchange。即使 state 是 pending 产生前已签发的 stale state，只要 owner 当前存在 `revoke_pending`，callback 也会在向 X 发出 token request 前拒绝。同 owner 重授权更新原记录；跨 owner 同 X 账号返回 409 `x_account_owned_by_other`，且不能改变原记录或 Token。

## POST /api/x-accounts/{id}/verify

请求体 `{}`。只允许当前 owner；非本人记录按不存在处理。账号为 `revoke_pending` 时返回 409 `x_disconnect_pending`，不 refresh、不请求 `/2/users/me`。其他状态必要时刷新/轮换 Token，再请求 X `/2/users/me` 更新账号资料快照。

```json
{"item": {"id": 1, "x_user_id": "123456789", "status": "active", "followers_count": 1200}}
```

## POST /api/x-accounts/{id}/logout

请求体 `{}`。只允许当前 owner，操作采用与 authorize/callback 一致的 owner→account 锁顺序，并在锁内二次执行 owner 联合过滤，避免 pending 与 OAuth token exchange 交错产生远端孤儿 Token。

成功语义：

1. 在首次远端调用前持久化 `revoke_pending`；该状态禁止 verify 与新 authorize，但允许当前 owner 再次 logout。
2. 读取本地 Token，固定先 revoke Access Token、最后 revoke Refresh Token；已撤销响应按幂等完成处理。
3. 任一远端调用失败时返回 502 `x_disconnect_failed`，保留 live Token 文件与 `revoke_pending`，仅更新脱敏错误信息。
4. 只有两项远端调用均成功/幂等后，才删除 live Token 及旧 `.<token filename>.*.disconnecting` tombstone；本地删除失败同样返回 502 并保持 `revoke_pending`。
5. 本地凭证全部清理成功后才置为 `disconnected`，记录 `disconnected_at` 与 `disconnected_by_*`，并保留账号资料、owner、授权时间和审计历史。

```json
{
  "item": {
    "id": 1,
    "x_user_id": "123456789",
    "status": "disconnected",
    "disconnected_at": "2026-07-14T04:30:00Z"
  }
}
```

远端网络/上游或本地删除失败返回 502 `x_disconnect_failed`，live Token 保留、状态为 `revoke_pending`；owner 可重试 logout。已是 `disconnected` 的重复 logout 幂等清理可能残留的 live Token/tombstone。Sidecar 每次启动也会对所有 `disconnected` 行执行相同残留清理。该操作只退出本应用授权，不退出 x.com 网站登录。

## GET /api/admin/x-accounts

仅 admin。返回所有 owner（包括 `revoke_pending`、`disconnected` 及 owner 尚未回填的 legacy 记录）的账号快照；响应结构与个人列表相同，但 `total` 是全局总数。主 API、Nginx 和页面 fetch 均 no-store。

可选 owner 为空的 legacy 项必须显式返回空 owner 字段，不能自动归给请求 admin。

## POST /api/admin/x-accounts/{id}/verify

仅 admin，请求体 `{}`。可同步任意非 `revoke_pending`/`disconnected` 账号的 Token 状态和 `/2/users/me` 资料快照；不能改变 owner，不能执行 logout。pending 时返回 `x_disconnect_pending`。成功响应与 owner verify 相同，审计 actor 为当前 admin。

## X 官方上游接口

### GET https://api.x.com/2/users/me

- Method：GET。
- Auth：`Authorization: Bearer <USER_ACCESS_TOKEN>`，必须是 OAuth 2.0 PKCE 或 OAuth 1.0a User Context；App-only 不支持。
- OAuth 2.0 scope：`tweet.read users.read`，当前五项授权已覆盖。
- Query：

```text
user.fields=profile_image_url,public_metrics,created_at,verified,protected,location
```

`id/name/username` 是默认字段，无需放入 query。`public_metrics` 作为整体返回，不能只请求子字段；快照至少读取 `followers_count/following_count/tweet_count/listed_count`，并兼容上游可能返回的 `like_count/media_count`。可选字段缺失时保留 `null` 或已有安全快照。

官方参考：[Get my User](https://docs.x.com/x-api/users/get-my-user)、[Authenticated User Quickstart](https://docs.x.com/x-api/users/lookup/quickstart/authenticated-lookup)。

### POST https://api.x.com/2/oauth2/revoke

- Method：POST。
- Header：`Content-Type: application/x-www-form-urlencoded`。
- 当前服务是 confidential client，使用 `Authorization: Basic <base64(client_id:client_secret)>`。
- Body：`token=<access_token-or-refresh_token>`。
- Access Token 与 Refresh Token 分别请求，固定 Access 先、Refresh 最后；官方示例没有 `token_type_hint`，实现不依赖该参数。
- 这是 App 撤销 Token，不是 X 网站账号 logout。官方没有承诺一个 Token 被撤销会级联撤销另一个。

官方参考：[OAuth 2.0 Authorization Code Flow with PKCE - Revoke Token](https://docs.x.com/fundamentals/authentication/oauth-2-0/user-access-token)。

## 错误码

| HTTP | error | 含义 |
| --- | --- | --- |
| 400 | `invalid_request` | 请求参数或 OAuth state 无效 |
| 401 | `unauthorized` | 未登录 AI 后台 |
| 403 | `permission_denied` / `admin_required` / `x_admin_required` / `cookie_auth_required` / `same_origin_required` | 模块、admin、Cookie 或来源不允许 |
| 404 | `x_account_not_found` | 记录不存在，或 owner 操作了非本人记录 |
| 409 | `x_account_owned_by_other` | callback 识别到该 X 账号已属于其他 owner |
| 409 | `x_disconnect_pending` | owner 有退出授权待完成：禁止 authorize/callback token exchange/verify，应重试 logout |
| 409 | `x_token_missing` / `x_token_revoked` / `x_identity_mismatch` | Token 缺失、授权失效或 Token 属主不匹配 |
| 502 | `x_disconnect_failed` | X revoke 或本地凭证清理失败；状态保持 `revoke_pending` |
| 502 | `x_upstream_error` | X token/user API 请求失败 |
| 503 | `x_oauth_not_configured` / `x_accounts_unavailable` | OAuth 或 sidecar 配置/运行异常 |

浏览器只接收白名单错误码和固定脱敏文案，不透传 X 响应正文。

## 公共回调

`GET /x-oauth/callback?code=...&state=...` 由 sidecar 处理，成功/失败均 302 回 `/x-accounts.html`。Callback query 不写入 Nginx access log 或 sidecar日志；跨 owner 冲突只回安全错误码。

## 内部 API

仅 `127.0.0.1:8810` 可达，并要求 `Authorization: Bearer <internal-token>`。V2 internal contract 必须显式携带由主后台生成的 actor，并区分 owner query/verify/logout 与 admin query/verify；不能信任浏览器提交的 owner 或 role。Authorize、callback 与 logout 采用统一 owner→account 锁序；callback 的 owner 锁覆盖 pending precheck 和 token exchange。Nginx 只公开 sidecar `/health` 与 `/callback`，其余 `/x-oauth/*` 返回 404。
