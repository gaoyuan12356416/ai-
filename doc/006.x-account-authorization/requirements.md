# 006.X账号授权管理 需求与技术设计

## 背景

X 自动发帖需要 OAuth 2.0 PKCE 用户授权。当前服务器已有单账号回调服务，但 AI 自动后台没有授权入口、账号列表、状态与时间信息，也不支持多账号授权。

## 目标

- 在 AI 自动后台新增“X账号授权”模块。
- 支持从已登录的 Feishu 后台会话发起 X OAuth 授权。
- 支持多个 X 账号，同一账号重复授权时更新原记录。
- 展示脱敏账号信息、授权时间、更新时间、Token 状态、权限与最近校验结果。
- Client Secret、Access Token、Refresh Token 不进入浏览器、AI 主后台数据库、日志或 Git。

## 范围

### 包含

- 模块权限 `x_accounts`，管理员默认可用，普通用户需授权。
- 独立页面 `/x-accounts.html`，复用公共 QuickNav 与 UiTopbar。
- AI 后台代理 API：配置、列表、发起授权、主动校验。
- X OAuth sidecar 多账号 SQLite 元数据、一次性 state、Token 文件存储。
- OAuth scopes：`tweet.read tweet.write users.read offline.access media.write`。
- 保留既有 Callback URI：`https://ai.yingliangads.com/x-oauth/callback`。
- 授权与校验审计日志，不记录任何密钥或 Token。

### 不包含

- 本需求不实现自动发帖计划、内容生成或媒体发布。
- 不展示或导出 Access Token、Refresh Token、Client Secret。
- 不提供账号删除、撤销授权、禁用按钮；避免误操作，后续另行评审。
- 不声称能够获得 X 账号自身的“最近登录 X 时间”；页面展示首次授权、最近授权、Token 刷新、最近校验与更新时间。

## 用户故事 / 业务规则

1. 有 `x_accounts` 权限的 Feishu 登录用户可以点击“授权新的 X 账号”。
2. 普通 API Token 不能代替 Feishu Cookie 发起或读取 X 授权。
3. OAuth state 一次性使用、10 分钟过期，并持久化以支持服务重启。
4. 授权完成后调用 `/2/users/me` 获取账号信息并按 `x_user_id` upsert。
5. 五项 scope 缺失任何一项时状态为 `scope_missing`。
6. Access Token 过期但 Refresh Token 可用时列表状态为 `refresh_required`；主动校验会刷新并保存轮换后的 Refresh Token。
7. Token 不存在、被撤销或 X API异常时分别显示 `token_missing`、`revoked`、`error`。
8. 打开列表不自动逐账号请求 X API；用户主动点击“校验状态”才调用，控制成本与延迟。
9. 所有时间字段使用 ISO 8601 UTC `...Z`，列表与授权响应禁止缓存。
10. Cookie写操作要求 JSON并校验同源 Origin/Referer；同账号刷新串行执行。
11. 五项必需 scope 是代码固定下限，环境变量只能追加不能删减；主动校验必须确认 `/2/users/me` 的用户ID与记录一致。

## 交互与流程

1. 用户进入 `/x-accounts.html`，页面读取 `/api/ui/topbar` 并校验 `x_accounts` 权限。
2. 点击授权，AI API调用 loopback sidecar `/internal/authorize`。
3. sidecar 持久化 state/PKCE verifier，返回 X 授权 URL。
4. 浏览器跳转 X，同意后回调既有 `/x-oauth/callback`。
5. sidecar 校验 state、换 Token、获取账号、保存 Token 与元数据，再跳回页面。
6. 页面重新加载脱敏账号列表。

## 技术设计

### 影响模块

- `app.py`：权限和受 Cookie 保护的代理 API。
- `features/x_accounts/oauth_service.py`：OAuth、多账号存储、内部 API。
- `features/x_accounts/client.py`：AI 后台 loopback 客户端。
- `static/x-accounts.html`：授权管理页面。
- `static/quick-nav.js`、`static/navigation.json`：导航。
- `deploy/x-post-automation.service`、`.env.example`：服务与配置。
- Nginx：仅公开 callback/health，拒绝公开 internal API。

### 数据结构

sidecar 数据库 `/var/lib/x-post-automation/accounts.sqlite3`，目录 `0700`、数据库 `0600`。

`x_authorized_account`：账号资料、scope、状态、首次/最近授权、Token 到期、刷新/校验/更新时间、授权操作人、最近错误；不存 Token 明文。

`x_oauth_state`：state 哈希、PKCE verifier、操作人、创建/过期时间；一次性消费。

`x_oauth_event`：只记录授权/校验的 started/completed/failed、操作人、账号ID和脱敏错误码；不保存 URL、state、code或 Token。

Token：`/var/lib/x-post-automation/tokens/<x_user_id>.json`，原子写入，权限 `0600`。

### API / 接口

- `GET /api/x-accounts/config`
- `GET /api/x-accounts`
- `POST /api/x-accounts/authorize`
- `POST /api/x-accounts/{id}/verify`
- sidecar private：`GET /internal/config`、`GET /internal/accounts`、`POST /internal/authorize`、`POST /internal/accounts/{id}/verify`
- sidecar public：`GET /health`、`GET /callback`

### 异常与边界

- 未登录 `401`，无权限或 API Token 登录 `403`。
- X 配置缺失返回 `x_oauth_not_configured`。
- state 错误、过期、重复使用均拒绝并回到页面显示失败。
- X token/user API错误只保存截断后的脱敏错误，不保存请求凭证。
- 内部 API必须同时满足 loopback 监听与 internal token。

## 验收标准

- 页面可从导航进入并遵循 Feishu 权限。
- 可完成真实 X OAuth，授权账号出现在列表。
- 列表包含账号、状态、五项权限、首次/最近授权、Token 到期、最近刷新、最近校验、更新时间、授权人。
- 重复授权同一 X 账号不新增重复记录。
- 主后台 API与页面响应不包含 Secret/Token/authorization code/verifier。
- 生产 callback URI不变，服务重启后数据仍在。

## 风险与待确认

- X API按量计费，主动校验会产生读取请求；本期不自动轮询。
- Callback URI 必须与 X Developer Console 完全一致（用户已确认配置完成）。
- 生产 `app.py` 是无 Git 的复合单体，部署前必须备份并以 live baseline 精确合并。

## 变更记录

- 2026-07-14：初稿；确定 sidecar 持有密钥/Token、AI 后台仅代理脱敏数据。
