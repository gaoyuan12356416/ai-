# 006.X账号授权管理 需求与技术设计

## 背景

X 自动发帖依赖 OAuth 2.0 PKCE 用户授权。V1 已在 AI 自动后台上线 X OAuth sidecar、授权入口、全量账号列表和主动校验，并已有 1 个生产授权账号及对应 Token。当前页面把“个人授权操作”和“后台全量账号管理”混在一起，同时列表、校验和按 `x_user_id` upsert 都是全局语义：普通有模块权限的用户可能看到或操作他人的账号，同一 X 账号被另一后台用户重新授权时还可能覆盖原属主的 Token 与归属信息。

V2 必须在不丢失现有生产账号、Token 和审计记录的前提下，拆分个人授权页与管理员列表页，并把后台属主边界升级为 `tenant_key + user_id`。

## 目标

- `/x-accounts.html` 作为个人授权页，只展示和操作当前后台用户自己的多个 X 账号。
- 个人授权页支持新增授权、主动校验和对单个账号“退出授权”。
- 新增 `/x-account-list.html` 管理员全量列表页，展示所有 X 账号、属主、粉丝量、名称、X 主页链接、授权状态及关键时间。
- 所有 owner 读写都以 `owner_tenant_key + owner_user_id` 为联合边界；`user_id` 单独匹配不构成授权。
- 授权 callback 与主动校验均通过 X `GET /2/users/me` 更新账号基础资料快照。
- 保留生产现有 1 条账号记录和对应 Token，并安全回填 legacy owner。
- Client Secret、Access Token、Refresh Token 不进入浏览器、AI 主后台数据库、日志或 Git。

## 范围

### 包含

- 模块权限 `x_accounts`：控制个人授权页；管理员通过现有 admin 角色访问全量列表页和 admin API。
- 个人页 `/x-accounts.html`：仅本人账号，支持多个账号授权、本人账号校验、本人单账号退出授权。
- 管理员页 `/x-account-list.html`，导航 key `xAccountList`：导航仅 admin 可见；静态页面壳可返回 200，但非 admin 只显示权限门且无法请求数据，展示全量账号与同步操作均依赖 admin API。
- owner API 与 admin API 分离，sidecar 每次请求都接收并校验脱敏 actor。
- SQLite 元数据新增 owner、账号资料快照与退出授权字段；Token 仍独立存储。
- OAuth scopes 固定下限：`tweet.read tweet.write users.read offline.access media.write`。
- 保留 Callback URI：`https://ai.yingliangads.com/x-oauth/callback`。
- X OAuth 2.0 Token 撤销：`POST https://api.x.com/2/oauth2/revoke`。
- 授权、校验、管理员同步和退出授权审计；不记录任何密钥、Token、code、state 或 verifier。

### 不包含

- 本需求不实现自动发帖计划、内容生成或媒体发布。
- 不展示或导出 Access Token、Refresh Token、Client Secret。
- 不允许管理员在全量列表页代替账号属主发起 OAuth 或强制退出授权；管理员本期只有全量查看和同步能力。
- “退出授权”只撤销本应用保存的 X OAuth Token 并清理本地凭证，不会让用户退出 x.com 网站或其他 X 客户端。
- 不声称能够获得 X 账号自身的“最近登录 X 时间”；页面展示首次/最近授权、资料同步、Token 刷新、最近校验、退出授权与更新时间。

## 用户故事 / 业务规则

1. 有 `x_accounts` 权限的 Feishu Cookie 用户可以在个人页授权自己的多个 X 账号。
2. 个人列表、个人校验和个人退出授权必须同时匹配当前会话的 `tenant_key` 与 `user_id`；不同租户出现相同 `user_id` 时仍视为不同 owner。
3. 管理员本人进入个人页时也只看到自己的账号；只有 `/x-account-list.html` 与 `/api/admin/x-accounts*` 返回全量数据。
4. 普通 API Token 不能读取或操作 X 授权；个人接口要求 Feishu Cookie，admin 接口还必须通过 `_require_admin()`。
5. OAuth state 一次性使用、10 分钟过期并持久化；state 必须保存 `actor_tenant_key`、`actor_user_id` 及展示信息，以便 callback 恢复 owner。
6. 授权完成后调用 X `/2/users/me`，按全局唯一 `x_user_id` 判断账号归属：
   - 不存在时，以当前 `tenant_key + user_id` 创建账号。
   - 已属于当前 owner 时，更新 Token、最近授权时间和资料快照，不新增重复记录。
   - 已属于其他 owner 时返回 `x_account_owned_by_other`，不得覆盖原 row、owner 或 Token 文件。
   - legacy owner 尚未回填时不得仅按 `user_id` 自动放开；该记录仅 admin 可见，须按部署迁移规则处理。
7. Callback 和 verify 保存 X 默认返回的 `id/name/username`，并请求 `profile_image_url/public_metrics/created_at/verified/protected/location` 作为基础资料快照。`public_metrics` 中保存粉丝数、关注数、发帖数和被列入列表数；若上游返回 `like_count/media_count` 也以可选值保存。
8. X 主页超链接仅在 `username` 匹配 `[A-Za-z0-9_]{1,50}` 时由 `https://x.com/<username>` 构造；超过 50 字符或包含其他字符时不生成链接。X 返回的 `url` 是用户填写的网站，不能当作 X 主页。
9. 打开列表不自动逐账号请求 X API；callback、本人主动校验和管理员主动同步时才更新资料，控制调用成本与延迟。
10. 五项 scope 缺失任何一项时状态为 `scope_missing`；Access Token 过期但 Refresh Token 可用时为 `refresh_required`；Token 缺失、被撤销或上游异常分别显示现有安全状态。
11. 单账号退出授权必须在账号锁内执行，并再次校验 owner。首次开始远端撤销前先把数据库状态持久化为 `revoke_pending`；服务端作为 confidential client，用 Basic `base64(client_id:client_secret)` 调用 X revoke 接口，固定先撤销 Access Token、最后撤销 Refresh Token。
12. 远端任一 revoke 出现网络或上游失败时返回 502 `x_disconnect_failed`，保留 live Token 文件并保持 `revoke_pending`，仅记录脱敏错误。只要同一 owner 任一账号 pending，该 owner 的 verify 与新 authorize 均返回 409 `x_disconnect_pending`；pending 前已签发的 stale OAuth state callback 也必须在 token request 前拒绝。owner 只能重试 logout 继续完成退出授权。
13. 只有 Access/Refresh 两项远端撤销均成功或被上游按幂等完成处理后，才删除 live Token 与旧 `.<token filename>.*.disconnecting` tombstone，再将状态置为 `disconnected` 并写入 `disconnected_at` 与 `disconnected_by_*`。本地删除失败也保持 `revoke_pending` 并返回 `x_disconnect_failed`。
14. Sidecar 每次启动完成存储初始化后，必须为数据库中已是 `disconnected` 的账号清理残留 live Token 和旧 `.disconnecting` tombstone，不能把凭证恢复为 active。
15. `disconnected` 账号保留脱敏元数据和审计历史，可由原 owner 重新授权恢复；admin 全量列表仍可看到该记录。
16. 所有时间字段使用 ISO 8601 UTC `...Z`；X 配置、个人列表、admin 列表及相关写操作响应都必须 `no-store`，admin 页面请求也显式使用 `cache: no-store`。
17. Cookie 写操作要求 JSON 并校验同源 Origin/Referer；authorize/callback/logout 采用一致的 owner→account 锁序，callback 的 owner 锁覆盖 pending precheck 与 token exchange。callback 持锁换取并写入新 Token 时 logout 必须等待，随后只撤销这组新 Token，避免铸造远端孤儿凭证；同一 `x_user_id` 的 callback、verify、admin verify 与 disconnect 继续串行执行。
18. 必需 scope 是代码固定下限，环境变量只能追加不能删减；校验必须确认 `/2/users/me` 的用户 ID 与记录一致。

## 交互与流程

### 个人授权页

1. 用户进入 `/x-accounts.html`，页面读取 `/api/ui/topbar` 并校验 `x_accounts` 权限。
2. 页面请求 `/api/x-accounts`，服务端从 Cookie 会话提取 `tenant_key + user_id`，只返回本人记录。
3. 点击“授权新的 X 账号”，主 API 把完整 owner actor 传给 loopback sidecar `/internal/authorize`；owner 任一账号 pending 时拒绝创建新 state。
4. sidecar 持久化 state/PKCE verifier 与 owner，浏览器跳转 X；用户同意后回调 `/x-oauth/callback`。
5. sidecar 在 owner 锁内重新检查 pending；stale state 命中 pending 时在 token request 前拒绝。否则锁覆盖换 Token、`/2/users/me`、owner 冲突检查与保存，logout 使用同一锁序等待后再撤销最新凭证。
6. 点击“校验状态”只校验本人账号；点击“退出授权”二次确认后调用本人 logout API。处理中/失败后显示 `revoke_pending` 与“重试退出”，禁用校验；全部完成后显示 `disconnected`。

### 管理员全量列表

1. 管理员从导航 `xAccountList` 进入 `/x-account-list.html`。
2. 页面请求 `/api/admin/x-accounts`，仅 admin 可返回全量账号及 owner/资料/状态快照。
3. 管理员可点击“同步”调用 `/api/admin/x-accounts/{id}/verify`；操作进入审计，但不改变 owner。
4. 非 admin 即使知道页面 URL 也只能看到无数据的权限门；全量 API 返回 403，按记录 ID 的 owner API 返回 404，不能读取或同步全量账号。

## 技术设计

### 影响模块

- `app.py`：owner actor、个人/管理员路由、同源校验、审计与错误白名单。
- `features/x_accounts/oauth_service.py`：owner 隔离、profile 快照、logout/revoke、legacy schema 迁移。
- `features/x_accounts/client.py`：owner/admin 查询、校验和 logout 的 loopback 契约。
- `static/x-accounts.html`：个人授权管理页。
- `static/x-account-list.html`：管理员全量列表页。
- `static/quick-nav.js`、`static/navigation.json`：个人入口和 admin-only `xAccountList` 导航。
- `deploy/*`：生产 schema/owner 回填、服务、Nginx 和回滚。

### 数据结构

sidecar 数据库 `/var/lib/x-post-automation/accounts.sqlite3`，目录 `0700`、数据库 `0600`。

`x_authorized_account` 保留 V1 字段，并以幂等 additive migration 增加：

- owner：`owner_tenant_key`、`owner_user_id`、`owner_name`、`owner_email`。
- X 资料：保留 `username`、`display_name`、`profile_image_url`，新增 `location`、`x_created_at`、`verified`、`protected`、`profile_synced_at`；API 兼容别名为 `last_profile_sync_at`，`profile_url` 运行时由 username 构造。
- public metrics：`followers_count`、`following_count`、`tweet_count`、`listed_count`，以及上游存在时的可选 `like_count`、`media_count`。
- 退出授权：`disconnected_at`、`disconnected_by_tenant_key`、`disconnected_by_user_id`、`disconnected_by_name`。

状态中新增终态前置状态 `revoke_pending`：它表示远端或本地清理尚未全部完成，不得根据 Token 文件重新投影为 active，也不得执行 verify。

`x_user_id` 继续全局唯一；增加 owner 联合索引。任何 owner 查询与写操作都必须在 SQL/服务层同时带 `owner_tenant_key` 和 `owner_user_id`，不能只依赖前端过滤。

`x_oauth_state` 增加 `actor_tenant_key`；`x_oauth_event` 增加 actor tenant/owner/操作类型所需脱敏字段。Token 仍位于 `/var/lib/x-post-automation/tokens/<x_user_id>.json`，原子写入、权限 `0600`。

### Legacy 生产迁移

1. 迁移前在线备份 SQLite 和整个 Token 目录，并记录现有 1 条账号记录及对应 Token 文件的非敏感 hash/权限。
2. 先幂等增加 V2 列，不重建或删除 V1 表，不改现有 `id`、`x_user_id`、`token_store_key`。
3. 使用 `scripts/backfill_x_account_owners.py` 处理 owner 为空的 legacy 记录；脚本默认 dry-run，默认读取 sidecar 数据库 `/var/lib/x-post-automation/accounts.sqlite3` 与主库 `/root/drama_material_service/data/drama_material_jobs.sqlite3`。
4. 脚本以 `authorized_by_user_id` 到主库 `drama_admin_user` 匹配；只有恰好一条且 tenant 非空时才可回填 `owner_*`。零匹配或多匹配保持 `owner_tenant_key` 为空，仅 admin 可见，绝不按 `user_id` 跨租户放开。
5. 上线门槛先运行 dry-run `python scripts/backfill_x_account_owners.py --require-all-resolved`，确认全部可唯一解析后才运行 `python scripts/backfill_x_account_owners.py --apply --require-all-resolved`；存在未解析项时退出码为 2 并阻断部署。
6. Apply 使用事务与条件更新；迁移后断言原记录数、账号 ID、Token 文件 hash/权限均未变化，再允许 owner 页面读写。

### API / 接口

- 个人：`GET /api/x-accounts/config`、`GET /api/x-accounts`、`POST /api/x-accounts/authorize`、`POST /api/x-accounts/{id}/verify`、`POST /api/x-accounts/{id}/logout`。
- 管理员：`GET /api/admin/x-accounts`、`POST /api/admin/x-accounts/{id}/verify`。
- 页面：`/x-accounts.html`、`/x-account-list.html`。
- sidecar private：owner/admin query、authorize、verify、logout 对应 `/internal/*`；都要求 loopback + internal bearer。
- sidecar public：仅 `GET /health`、`GET /callback`。

### X 官方接口契约

- 资料：`GET https://api.x.com/2/users/me`，`Authorization: Bearer <USER_ACCESS_TOKEN>`，只支持 User Context；`id/name/username` 为默认字段，实际 query 请求 `user.fields=profile_image_url,public_metrics,created_at,verified,protected,location`。
- 撤销：`POST https://api.x.com/2/oauth2/revoke`，`Content-Type: application/x-www-form-urlencoded`；confidential client 使用 `Authorization: Basic <base64(client_id:client_secret)>`，表单体为 `token=<access-or-refresh-token>`。
- 官方没有承诺撤销一个 Token 会级联撤销关联 Token，因此本服务分别调用两次，并固定 Access Token 先、Refresh Token 最后；在最后一个可用于续期的凭证撤销前，失败仍可安全重试。

### 异常与边界

- 未登录 401；无模块权限、非 admin 或 API Token 访问返回 403。
- owner 按 ID 操作他人记录时返回 404，避免确认记录是否存在；跨 owner callback 返回 409 `x_account_owned_by_other`。
- X revoke 网络/上游或本地凭证清理失败返回 502 `x_disconnect_failed`，状态保持 `revoke_pending`，不得误标 `disconnected`。
- Owner 任一账号为 `revoke_pending` 时，verify、新 authorize 及 stale state callback 均返回 409 `x_disconnect_pending`；callback 必须在 token request 前拒绝，但 owner logout 必须允许重试。
- X 配置缺失、state 错误/过期/重放、Token/user API 错误沿用脱敏错误契约。
- 内部 API 必须同时满足 loopback 监听与 internal token；Nginx 不公开 `/internal/*`。

## 验收标准

- 个人页与 admin 全量列表页完全分开，导航和页面权限符合角色。
- 同一 owner 可授权多个不同 X 账号；个人列表只返回当前 `tenant_key + user_id` 的记录。
- 普通用户不能通过枚举 ID 查看、verify 或 logout 他人记录；不同 tenant 的相同 user ID 也不能越权。
- admin 能看到所有 X 账号的名称、用户名、头像、X 主页链接、粉丝/关注/发帖/列表数、认证信息、owner、授权状态和时间，并可主动同步。
- 同一 X 账号由当前 owner 重授权时原行更新；其他 owner 重授权时返回冲突且原 Token hash、owner 和授权时间不变。
- 本人 logout 固定按 Access、Refresh 顺序撤销；开始远端调用前进入 `revoke_pending`。失败时 live Token 保留、verify 被禁止且 logout 可重试；只有远端与本地清理全部完成后 Token 不再存在且状态为 `disconnected`。
- Owner pending 期间不能发起新 authorize；已有 stale callback 不产生 token request。callback/token exchange 与 logout 并发时 logout 等待 callback 写入完成，并撤销 callback 刚写入的新 Access/Refresh Token。
- 生产 legacy 1 条记录和对应 Token 在 schema/owner 回填后保持可用，且 owner 唯一回填证据可审计。
- Callback/verify 更新资料快照；打开列表不触发逐账号 X API 调用。
- Admin API、Nginx 和页面请求均为 no-store；个人/admin 页面同时展示 Token 刷新、最近校验/资料同步、更新时间和退出授权时间。
- 合法 X username 上限为 50 字符；超长或含不安全字符时不得生成可点击主页链接。
- API、页面、日志和审计不包含 Secret/Token/code/verifier。

## 风险与待确认

- X API 按量计费；资料只在 callback/主动同步更新，管理员列表展示最后成功快照。
- OAuth revoke 是本应用凭证撤销，不等于 X 网站 logout；UI 必须明确提示。
- Legacy owner 唯一匹配是上线门槛；无法唯一匹配时必须保持 admin-only，不允许便利性兜底。
- 生产 `app.py` 是复合单体，部署前必须备份并以 live baseline 精确合并。

## 变更记录

- 2026-07-14：V1 初稿；确定 sidecar 持有密钥/Token、AI 后台仅代理脱敏数据。
- 2026-07-14：V2 拆分个人授权页与 admin 全量列表；新增 owner 联合隔离、单账号退出授权、资料快照和 legacy 生产迁移契约。
