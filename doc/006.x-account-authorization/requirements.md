# 006.X账号授权管理 需求与技术设计

> 当前版本：V3（2026-07-14，本地软停用）

## 背景

X 自动发帖依赖 OAuth 2.0 PKCE 用户授权。V1 已在 AI 自动后台上线 X OAuth sidecar、授权入口、全量账号列表和主动校验，并已有 1 个生产授权账号及对应 Token。当前页面把“个人授权操作”和“后台全量账号管理”混在一起，同时列表、校验和按 `x_user_id` upsert 都是全局语义：普通有模块权限的用户可能看到或操作他人的账号，同一 X 账号被另一后台用户重新授权时还可能覆盖原属主的 Token 与归属信息。

V2 已在不丢失现有生产账号、Token 和审计记录的前提下拆分个人授权页与管理员列表页，并把后台属主边界升级为 `tenant_key + user_id`。V3 将 `/logout` 的产品语义调整为“后台本地软停用”：不再调用 X revoke，不读取或删除 Token；账号标记为 `disabled` 后不得进入任何发布流程，原 owner 重新授权同一账号可恢复 `active`。

## 目标

- `/x-accounts.html` 作为个人授权页，只展示和操作当前后台用户自己的多个 X 账号。
- 个人授权页支持新增授权、主动校验和对单个账号“后台停用”；为兼容现有调用，服务端路由名称继续保留 `/logout`。
- 新增 `/x-account-list.html` 管理员全量列表页，展示所有 X 账号、属主、粉丝量、名称、X 主页链接、授权状态及关键时间。
- 所有 owner 读写都以 `owner_tenant_key + owner_user_id` 为联合边界；`user_id` 单独匹配不构成授权。
- 授权 callback 与主动校验均通过 X `GET /2/users/me` 更新账号基础资料快照。
- 保留生产现有 1 条账号记录和对应 Token，并安全回填 legacy owner。
- 只有持久化/投影状态均为 `active` 的账号可以通过 `publish_credentials` 上下文取得发布凭证；敏感凭证只 yield `access_token` 字符串（另有脱敏账号对象），停用必须与发布共用账号锁。实际 X Post 发布尚未接入。
- Client Secret、Access Token、Refresh Token 不进入浏览器、AI 主后台数据库、日志或 Git。

## 范围

### 包含

- 模块权限 `x_accounts`：控制个人授权页；管理员通过现有 admin 角色访问全量列表页和 admin API。
- 个人页 `/x-accounts.html`：仅本人账号，支持多个账号授权、本人账号校验、本人单账号本地软停用。
- 管理员页 `/x-account-list.html`，导航 key `xAccountList`：导航仅 admin 可见；静态页面壳可返回 200，但非 admin 只显示权限门且无法请求数据，展示全量账号与同步操作均依赖 admin API。
- owner API 与 admin API 分离，sidecar 每次请求都接收并校验脱敏 actor。
- SQLite 元数据包含 owner、账号资料快照与停用审计字段；Token 仍独立存储并在软停用后保留。
- OAuth scopes 固定下限：`tweet.read tweet.write users.read offline.access media.write`。
- 保留 Callback URI：`https://ai.yingliangads.com/x-oauth/callback`。
- V3 本地软停用：`status=disabled`，不调用 X revoke、不读取 Token、不删除 Token 或 tombstone。
- 授权、校验、管理员同步和后台停用审计；不记录任何密钥、Token、code、state 或 verifier。

### 不包含

- 本需求不实现自动发帖计划、内容生成或媒体发布。
- 不展示或导出 Access Token、Refresh Token、Client Secret。
- 不允许管理员在全量列表页代替账号属主发起 OAuth 或强制停用；管理员本期只有全量查看和同步能力。
- V3 不实现 X OAuth Token 的远端 revoke，也不让用户退出 x.com 网站或其他 X 客户端；如需在 X 侧撤销，须由用户在 X 安全/应用设置中另行操作。
- 不声称能够获得 X 账号自身的“最近登录 X 时间”；页面展示首次/最近授权、资料同步、Token 刷新、最近校验、后台停用与更新时间。

## 用户故事 / 业务规则

1. 有 `x_accounts` 权限的 Feishu Cookie 用户可以在个人页授权自己的多个 X 账号。
2. 个人列表、个人校验和个人软停用必须同时匹配当前会话的 `tenant_key` 与 `user_id`；不同租户出现相同 `user_id` 时仍视为不同 owner。
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
11. 单账号 `/logout` 必须采用 owner→account 锁序，在账号锁内再次校验 owner，并以一次本地数据库更新把状态写为 `disabled`；同时清空旧错误字段，写入 `disconnected_at` 与 `disconnected_by_*` 作为兼容的停用时间/操作人字段。
12. 软停用不得读取 Token 文件、不得调用 X revoke 或任何其他 X API、不得删除 Token 文件或旧 tombstone，也不得清空 `access_expires_at`。重复停用必须幂等，首次停用后的时间、Token 字节和权限保持不变。
13. 历史 `revoke_pending` 行属于 legacy 状态；owner 调用现有 `/logout` 时必须能直接转为 `disabled`，即使 Token 文件缺失或不可解析也不能阻断。V3 不再以 pending 阻止新 authorize/callback；同一 owner 重新授权后可恢复 `active`。如果 OAuth state 在停用前已由用户显式签发、callback 在停用后才完成，则该 callback 视为显式重新授权，最后写入的新 Token/资料胜出并恢复 `active`。
14. `disabled` 是本地终态，列表投影不得因 Token 文件仍存在而改回 `active`；verify 必须在读取 Token 或请求 `/2/users/me` 前返回 409 `x_account_disabled`。
15. 发布选择采用严格 allowlist：仅 `status=active` 且存在 Access Token 的账号能进入 `publish_credentials` 上下文；`disabled` 返回 `x_account_disabled`，其他非 active 状态返回 `x_account_not_publishable`。上下文的敏感输出只有 `access_token` 字符串，不 yield 完整 Token 字典或 Refresh Token，且不能序列化或返回浏览器。
16. 实际 X Post 发布尚未接入。未来发布调用必须留在同一个 `publish_credentials` context 内，context 必须在整个上游发布期间持有账号锁；不得先取出 `access_token` 再退出 context 发布。停用与发布并发时，已进入上下文的发布先完成，停用随后写入 `disabled`；停用完成后新的发布上下文不得取得凭证。
17. 历史 `disconnected` 状态继续兼容展示与清理：sidecar 启动或 owner 重复调用 `/logout` 时，只为 `disconnected` 行删除残留 live Token 和 `.disconnecting` tombstone并保持 `disconnected`；该清理绝不能作用于 `disabled`。原 owner 对 `disabled`/legacy `disconnected` 账号重新授权后可恢复 `active`。
18. Cookie 写操作要求 JSON 并校验同源 Origin/Referer；authorize/callback/logout 采用一致的 owner→account 锁序，同一 `x_user_id` 的 callback、verify、admin verify、publish 与停用串行。所有时间使用 ISO 8601 UTC `...Z`，相关响应必须 `no-store`；必需 scope 为代码固定下限，校验必须确认 `/2/users/me` 用户 ID 与记录一致。

## 交互与流程

### 个人授权页

1. 用户进入 `/x-accounts.html`，页面读取 `/api/ui/topbar` 并校验 `x_accounts` 权限。
2. 页面请求 `/api/x-accounts`，服务端从 Cookie 会话提取 `tenant_key + user_id`，只返回本人记录。
3. 点击“授权新的 X 账号”，主 API 把完整 owner actor 传给 loopback sidecar `/internal/authorize`；legacy `revoke_pending` 或 `disabled` 不阻止创建新的 state。
4. sidecar 持久化 state/PKCE verifier 与 owner，浏览器跳转 X；用户同意后回调 `/x-oauth/callback`。
5. sidecar 在 owner→account 锁内完成换 Token、`/2/users/me`、owner 冲突检查与保存；同 owner 对 `disabled` 账号重新授权时更新原行、覆盖保留的旧 Token 并恢复 `active`。
6. 点击“校验状态”只校验本人非停用账号；点击“停用账号”二次确认后仍调用本人 `/logout` API。成功后显示 `disabled`/“已停用”，Token 保留且不再参与后台发布；legacy `revoke_pending` 显示“完成停用”。

### 管理员全量列表

1. 管理员从导航 `xAccountList` 进入 `/x-account-list.html`。
2. 页面请求 `/api/admin/x-accounts`，仅 admin 可返回全量账号及 owner/资料/状态快照。
3. 管理员可点击“同步”调用 `/api/admin/x-accounts/{id}/verify`；操作进入审计，但不改变 owner。
4. 非 admin 即使知道页面 URL 也只能看到无数据的权限门；全量 API 返回 403，按记录 ID 的 owner API 返回 404，不能读取或同步全量账号。

## 技术设计

### 影响模块

- `app.py`：owner actor、个人/管理员路由、同源校验、审计与错误白名单。
- `features/x_accounts/oauth_service.py`：owner 隔离、profile 快照、本地软停用、`publish_credentials` 发布锁与 legacy schema/status 兼容。
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
- 停用审计（复用兼容字段）：`disconnected_at`、`disconnected_by_tenant_key`、`disconnected_by_user_id`、`disconnected_by_name`。

V3 现行本地终态为 `disabled`。`revoke_pending` 与 `disconnected` 仅作为 V2/历史数据兼容状态保留；两者和 `disabled` 都不得因 Token 文件存在而在列表投影中变回 `active`。API 运行时另返回 `publish_eligible`，仅当有效状态为 `active` 时为 `true`。

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
- V3 `/logout` 是纯本地软停用，不属于 X 上游接口契约；该路径不得向 `api.x.com` 发出请求。

### 异常与边界

- 未登录 401；无模块权限、非 admin 或 API Token 访问返回 403。
- owner 按 ID 操作他人记录时返回 404，避免确认记录是否存在；跨 owner callback 返回 409 `x_account_owned_by_other`。
- `disabled` verify 在任何 Token I/O 或 X 请求前返回 409 `x_account_disabled`；发布凭证 guard 对 `disabled` 返回同码，对其他非 active 状态返回 409 `x_account_not_publishable`。
- Legacy `revoke_pending` verify 仍返回 409 `x_disconnect_pending`；但它不再阻止 authorize/callback，并可通过 owner `/logout` 直接转为 `disabled`。
- 本地停用状态保存失败返回脱敏的 `x_accounts_unavailable`；失败前不得改动或删除 Token。
- Legacy `disconnected` 重复 logout 的残留 Token/tombstone 清理失败沿用 502 `x_disconnect_failed`，状态仍保持 `disconnected`，不得影响 `disabled` Token。
- X 配置缺失、state 错误/过期/重放、Token/user API 错误沿用脱敏错误契约。
- 内部 API 必须同时满足 loopback 监听与 internal token；Nginx 不公开 `/internal/*`。

## 验收标准

- 个人页与 admin 全量列表页完全分开，导航和页面权限符合角色。
- 同一 owner 可授权多个不同 X 账号；个人列表只返回当前 `tenant_key + user_id` 的记录。
- 普通用户不能通过枚举 ID 查看、verify 或 logout 他人记录；不同 tenant 的相同 user ID 也不能越权。
- admin 能看到所有 X 账号的名称、用户名、头像、X 主页链接、粉丝/关注/发帖/列表数、认证信息、owner、授权状态和时间，并可主动同步。
- 同一 X 账号由当前 owner 重授权时原行更新；其他 owner 重授权时返回冲突且原 Token hash、owner 和授权时间不变。
- 本人 `/logout` 只在本地写 `disabled`：不读取 Token、不调用 X、不删除 Token；重复调用幂等，Token hash/权限和授权到期元数据保持不变。
- Legacy `revoke_pending` 可直接软停用；`disabled` 不能 verify 或发布，原 owner 重新授权同一账号后恢复 `active` 并覆盖旧 Token。
- 实际发布尚未接入；未来发布只能在同一 `publish_credentials` context 内使用其 yield 的 `access_token`。上下文持锁期间并发停用等待，退出上下文后停用完成，后续发布被拒绝。
- 生产 legacy 1 条记录和对应 Token 在 schema/owner 回填后保持可用，且 owner 唯一回填证据可审计。
- Callback/verify 更新资料快照；打开列表不触发逐账号 X API 调用。
- Admin API、Nginx 和页面请求均为 no-store；个人/admin 页面同时展示 Token 刷新、最近校验/资料同步、更新时间和后台停用时间。
- 合法 X username 上限为 50 字符；超长或含不安全字符时不得生成可点击主页链接。
- API、页面、日志和审计不包含 Secret/Token/code/verifier。

## 风险与待确认

- X API 按量计费；资料只在 callback/主动同步更新，管理员列表展示最后成功快照。
- 软停用只约束本后台发布选择，X 侧 Token 仍可能有效；UI 必须明确提示“不调用 X 撤销接口、Token 继续保存在服务器”。
- Legacy owner 唯一匹配是上线门槛；无法唯一匹配时必须保持 admin-only，不允许便利性兜底。
- 生产 `app.py` 是复合单体，部署前必须备份并以 live baseline 精确合并。

## 变更记录

- 2026-07-14：V1 初稿；确定 sidecar 持有密钥/Token、AI 后台仅代理脱敏数据。
- 2026-07-14：V2 拆分个人授权页与 admin 全量列表；新增 owner 联合隔离、单账号退出授权、资料快照和 legacy 生产迁移契约。
- 2026-07-14：V3 将保留的 `/logout` 路由改为本地软停用；新增 `disabled`、严格 active 发布 guard、发布/停用锁序与 legacy `revoke_pending`/`disconnected` 兼容规则，远端 revoke 不再是现行要求。
