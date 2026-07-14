# X账号授权管理 API

> 当前版本：V3（2026-07-14，本地软停用）

## 页面与角色

| 页面 | 用途 | 权限 |
| --- | --- | --- |
| `/x-accounts.html` | 当前用户个人 X 授权管理：多账号授权、校验、后台停用 | Feishu Cookie + `x_accounts` |
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
  "publish_eligible": true,
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

状态至少包括：`active`、`refresh_required`、`scope_missing`、`token_missing`、`revoked`、`error`、`disabled`，并兼容历史 `revoke_pending`、`disconnected`。`disabled` 是 V3 本地终态；三种持久化停用/历史状态都不得因 live Token 文件仍存在而投影回 `active`。`publish_eligible` 仅当有效状态严格为 `active` 时为 `true`。

页面时间展示约定：个人页“刷新/校验”显示 `last_token_refresh_at`、`last_verified_at`，“更新/停用”显示 `updated_at`、`disconnected_at`；admin 页另将 `access_expires_at` 与刷新时间同列、`last_profile_sync_at` 与校验时间同列。V3 继续复用 `disconnected_*` 字段记录软停用时间与操作人，以保持 schema 兼容。

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

请求体 `{}`。服务端从 Cookie 生成 actor，禁止客户端提交或覆盖 owner。V3 不因 owner 的 `disabled` 或 legacy `revoke_pending` 记录阻止创建新的 OAuth state。成功响应：

```json
{
  "authorization_url": "https://x.com/i/oauth2/authorize?...",
  "callback_url": "https://ai.yingliangads.com/x-oauth/callback",
  "scopes": ["tweet.read", "tweet.write", "users.read", "offline.access", "media.write"],
  "expires_at": "2026-07-14T03:30:00Z"
}
```

Callback 恢复 state 中的 actor，owner 锁覆盖 token exchange，同一 `x_user_id` 的账号锁覆盖 Token 与资料保存。同 owner 重授权更新原记录；若原状态是 `disabled`、`revoke_pending` 或 legacy `disconnected`，成功回调会覆盖旧 Token、清空兼容停用字段并恢复 `active`。OAuth state 即使在停用前签发，只要用户在停用后完成该 callback，也视为显式重新授权，采用最后写入胜出语义。跨 owner 同 X 账号返回 409 `x_account_owned_by_other`，且不能改变原记录或 Token。

## POST /api/x-accounts/{id}/verify

请求体 `{}`。只允许当前 owner；非本人记录按不存在处理。账号为 `disabled` 时在读取 Token 前返回 409 `x_account_disabled`；legacy `revoke_pending` 返回 409 `x_disconnect_pending`，legacy `disconnected` 返回 409 `x_token_missing`。以上状态都不 refresh、不请求 `/2/users/me`。其他状态必要时刷新/轮换 Token，再请求 X `/2/users/me` 更新账号资料快照。

```json
{"item": {"id": 1, "x_user_id": "123456789", "status": "active", "followers_count": 1200}}
```

## POST /api/x-accounts/{id}/logout

请求体 `{}`。路由名为兼容现有前端和客户端继续保留，但 V3 语义是“后台本地软停用”，不是 X 侧 logout 或 OAuth revoke。只允许当前 owner，操作采用与 authorize/callback 一致的 owner→account 锁顺序，并在账号锁内再次执行 owner 联合过滤。

成功语义：

1. 不读取 Token 文件，不调用 `api.x.com`，不调用 X revoke，不删除 Token 或旧 tombstone。
2. 在锁内把账号状态直接写为 `disabled`，清空 `last_error_at/last_error`，并写入 `disconnected_at` 与 `disconnected_by_*` 作为兼容的停用审计字段。
3. 保留 `access_expires_at`、账号资料、owner、首次/最近授权时间、Token 文件字节与文件权限。
4. 已是 `disabled` 时重复调用幂等返回，不改首次停用时间或 Token。
5. Legacy `revoke_pending` 即使 Token 文件不可读，也可通过本路由直接转为 `disabled`。Legacy `disconnected` 保持历史终态；owner 重复调用本路由时只删除其残留 live Token/旧 tombstone，并仍返回 `disconnected`。

```json
{
  "item": {
    "id": 1,
    "x_user_id": "123456789",
    "status": "disabled",
    "publish_eligible": false,
    "disconnected_at": "2026-07-14T04:30:00Z"
  }
}
```

状态保存失败返回脱敏的 503 `x_accounts_unavailable`，且不得改动 Token。Sidecar 启动和 legacy `disconnected` 重复 logout 都会清理该历史行的残留 live Token/tombstone；清理失败返回 502 `x_disconnect_failed`，但绝不能清理 `disabled` 行的 Token。`disabled` 账号不再用于后台发布；如需恢复，原 owner 重新授权同一账号即可回到 `active`。

## GET /api/admin/x-accounts

仅 admin。返回所有 owner（包括 `disabled`、legacy `revoke_pending`/`disconnected` 及 owner 尚未回填的 legacy 记录）的账号快照；响应结构与个人列表相同，但 `total` 是全局总数。主 API、Nginx 和页面 fetch 均 no-store。

可选 owner 为空的 legacy 项必须显式返回空 owner 字段，不能自动归给请求 admin。

## POST /api/admin/x-accounts/{id}/verify

仅 admin，请求体 `{}`。可同步任意可校验账号的 Token 状态和 `/2/users/me` 资料快照；不能改变 owner，不能执行 logout。`disabled` 返回 `x_account_disabled`，legacy pending/disconnected 沿用 owner verify 的安全错误。成功响应与 owner verify 相同，审计 actor 为当前 admin。

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

### V3 本地停用边界

`POST /api/x-accounts/{id}/logout` 不对应任何 X 上游请求。当前服务不会通过该路由撤销 Access/Refresh Token；Token 继续以 `0600` 保存在 sidecar Token 目录，仅由严格 active 的发布上下文或重新授权流程使用。

## 错误码

| HTTP | error | 含义 |
| --- | --- | --- |
| 400 | `invalid_request` | 请求参数或 OAuth state 无效 |
| 401 | `unauthorized` | 未登录 AI 后台 |
| 403 | `permission_denied` / `admin_required` / `x_admin_required` / `cookie_auth_required` / `same_origin_required` | 模块、admin、Cookie 或来源不允许 |
| 404 | `x_account_not_found` | 记录不存在，或 owner 操作了非本人记录 |
| 409 | `x_account_owned_by_other` | callback 识别到该 X 账号已属于其他 owner |
| 409 | `x_account_disabled` | 账号已在后台停用，禁止 verify 或取得发布凭证；重新授权可恢复 |
| 409 | `x_account_not_publishable` | 账号有效状态不是严格 `active`，禁止取得发布凭证 |
| 409 | `x_disconnect_pending` | Legacy `revoke_pending` 账号禁止 verify；可调用 owner `/logout` 转为 `disabled` |
| 409 | `x_token_missing` / `x_token_revoked` / `x_identity_mismatch` | Token 缺失、授权失效或 Token 属主不匹配 |
| 502 | `x_disconnect_failed` | 仅 legacy `disconnected` 重复 logout 的历史凭证残留清理失败 |
| 502 | `x_upstream_error` | X token/user API 请求失败 |
| 503 | `x_oauth_not_configured` / `x_accounts_unavailable` | OAuth、sidecar 或本地停用状态保存异常 |

浏览器只接收白名单错误码和固定脱敏文案，不透传 X 响应正文。

## 公共回调

`GET /x-oauth/callback?code=...&state=...` 由 sidecar 处理，成功/失败均 302 回 `/x-accounts.html`。Callback query 不写入 Nginx access log 或 sidecar日志；跨 owner 冲突只回安全错误码。

## 内部 API

仅 `127.0.0.1:8810` 可达，并要求 `Authorization: Bearer <internal-token>`。V3 internal contract 必须显式携带由主后台生成的 actor，并区分 owner query/verify/logout 与 admin query/verify；不能信任浏览器提交的 owner 或 role。Authorize、callback 与 logout 采用统一 owner→account 锁序。`publish_credentials(account_id, actor, scope)` 是 sidecar 进程内上下文管理器，不是 HTTP API：仅 active 账号可进入，返回脱敏 account item，敏感凭证只 yield `access_token` 字符串，不返回完整 Token 字典或 Refresh Token。实际 X Post 发布尚未接入；未来调用方必须把整个上游发帖动作放在同一个 `with` context 内，不得把 `access_token` 带出上下文，以便账号锁覆盖发布；停用会等待该 context 退出。Nginx 只公开 sidecar `/health` 与 `/callback`，其余 `/x-oauth/*` 返回 404。
