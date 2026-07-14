# 部署文档

## 变更内容

V2 在现有 X OAuth sidecar 上新增：

- `/x-accounts.html` 个人授权页，只显示/操作本人多个 X 账号并支持退出授权。
- `/x-account-list.html` admin-only 全量列表页，导航 key `xAccountList`，支持管理员同步。
- `tenant_key + user_id` owner 联合隔离、admin 独立 API、跨 owner 重授权保护。
- `/2/users/me` 基础资料/public metrics 快照。
- X OAuth `revoke_pending` 可恢复状态机、Access-first/Refresh-last 双 revoke、`disconnected` 启动残留清理。
- V1 schema/生产账号及 Token 的无损迁移，以及默认 dry-run 的标准 owner 回填脚本。

## 配置项

主后台 `.env` 沿用：

```text
X_POST_AUTOMATION_INTERNAL_URL=http://127.0.0.1:8810
X_POST_AUTOMATION_INTERNAL_TOKEN=<server-only>
X_POST_AUTOMATION_INTERNAL_TIMEOUT=30
```

sidecar `/etc/x-post-automation.env` 继续保存现有 Client ID/Secret/Callback/scopes、`X_INTERNAL_TOKEN`、返回 URL 和 DB/Token 目录。所有真实值仅在服务器 root-only env；部署、测试和日志不得输出。

Callback 仍为：

```text
https://ai.yingliangads.com/x-oauth/callback
```

## 数据库变更

- sidecar 启动/迁移逻辑对 `/var/lib/x-post-automation/accounts.sqlite3` 执行幂等 additive migration，不重建 V1 表。
- `x_authorized_account` 增加 owner、X profile/public metrics、disconnect/remote revoke 字段和 owner 联合索引。
- `x_oauth_state` 增加 `actor_tenant_key`；审计事件补 tenant/owner 上下文。
- 不把 Token 移入 SQLite；原 Token 文件路径、文件名、权限和内容必须保持。
- 不修改主业务库表结构；`drama_admin_user` 仅用于 legacy owner 的只读唯一匹配。

## Legacy owner 回填

生产上线前预期已有 1 条 V1 `x_authorized_account` 与对应 Token 文件。迁移必须按以下规则执行：

1. 对 sidecar SQLite 做一致性备份，并复制整个 Token 目录；记录 row count、记录 ID、`x_user_id`、`token_store_key`、Token 文件权限和 SHA-256。Hash 可记录，Token 内容不可输出。
2. 停止/隔离 sidecar 写入后部署 V2 schema，先只执行幂等建列/索引；重复执行不得报错或改变业务数据。
3. 在 release 根目录先运行默认 dry-run 严格门槛：

   ```bash
   python scripts/backfill_x_account_owners.py --require-all-resolved
   ```

   默认读取 `/var/lib/x-post-automation/accounts.sqlite3` 与 `/root/drama_material_service/data/drama_material_jobs.sqlite3`，输出 JSON，不修改数据库；存在零/多匹配等未解析项时退出码为 2。
4. 只有每条 legacy 行按 `authorized_by_user_id` 在 `drama_admin_user` 恰好匹配一条且 tenant 非空时才允许 apply：

   ```bash
   python scripts/backfill_x_account_owners.py --apply --require-all-resolved
   ```

5. Apply 使用 `BEGIN IMMEDIATE`、guarded update 和更新数量复核；并发变化或数量不符必须整笔回滚。零/多匹配保持 `owner_tenant_key` 为空、仅 admin 可见，禁止人工按 `user_id` 兜底。
6. 回填后重新计算并比对 row count、记录 ID、x_user_id、token_store_key、Token SHA-256/权限；任何不一致立即停止部署并回滚。
7. 使用回填 owner 的真实 Cookie 验证个人列表能看到原账号，再验证其他 tenant/user 看不到。

禁止把真实账号标识、Token 或 Secret 写进本文档、Git、命令历史输出或工单。

## 部署步骤

1. 完成 BUG-004/005、pending 状态机、启动清理和 backfill CLI 的自动化/代码评审；执行 Python/JS/JSON 与 `git diff --check`，并完成 V1 DB 副本迁移演练。
2. GitHub-first：本地提交/推送，服务器检出精确 V2 commit；禁止从本地直接 SCP 未提交源码替代 GitHub。
3. 记录生产 live hash；创建包含 app、features、两个页面、导航、systemd、Nginx、两个 env、SQLite 在线备份和 Token 目录的带时间戳备份。
4. 确认无 callback/verify/logout 在途，短暂停止 `x-post-automation.service`，执行 schema migration、backfill dry-run 严格门槛和 apply；回填前后复核 Token hash。
5. 部署精确 release 中的 `app.py`、`features/x_accounts/*`、两个页面/导航、Nginx/systemd；同步 `/usr/share/nginx/html` 公网副本。
6. 保持现有 Client ID/Secret/internal token 不变且 env 为 `0600`；不在命令输出中回显。
7. 执行迁移后 Token/row/hash 断言；不通过则不启动 V2。
8. Python/JS/JSON/Nginx/systemd 检查通过后，先启动 sidecar，再重启主 API，最后 reload Nginx。
9. 运行 API、权限、浏览器、owner/admin、真实资料同步和日志泄漏验证。

## 验证步骤

### 服务与边界

- `x-post-automation.service`、`drama-material-api.service` active；local/public health 正常。
- 公网仅 `/x-oauth/health` 与 callback 可达；`/x-oauth/internal/*` 404。
- 未登录 401、API Token 403、无模块权限 403、非 admin 访问 admin API 403。
- Nginx callback 仍 `access_log off`；确认主 API 的 owner/admin 响应、Nginx admin exact/prefix 路由和页面 fetch 三层 no-store。

### Owner/Admin 隔离

- A=`t1+u1`、B=`t1+u2`、C=`t2+u1`：个人列表严格隔离。
- A 对 B/C 的 verify/logout ID 返回 404，且不产生 X 上游请求或数据变化。
- admin 全量页可见全部；admin verify 成功但 owner 不变；非 admin 无法看到页面数据。
- 跨 owner 重新授权相同 X 账号返回冲突；原 owner、Token hash 和时间不变。

### 资料与退出授权

- Callback/verify 后确认粉丝数、名称、头像、verified 等快照；只有 `[A-Za-z0-9_]{1,50}` username 生成 `https://x.com/<username>`，超长/非法值无链接。
- 刷新个人/admin 列表不产生 `/2/users/me` 请求。
- 两页展示刷新、校验/资料同步、更新和退出时间；admin 另展示 Token 到期时间。
- 经用户确认后执行一次真实退出授权：远端前进入 `revoke_pending`，Access 先、Refresh 最后；全部成功并清理 live Token/旧 tombstone 后才进入 `disconnected`。
- 注入任一 revoke 或本地删除失败：返回 `x_disconnect_failed`，live Token 保留且状态为 `revoke_pending`；verify 返回 `x_disconnect_pending`，重试 logout 可最终完成。
- Owner 任一账号 pending 时，新 authorize 返回 `x_disconnect_pending`；已有 stale state callback 必须在 token request 前拒绝。并发探针确认 authorize/callback/logout 使用 owner→account 锁序，无远端孤儿 Token。

### Legacy 保全

- 迁移前后原 1 条记录的 ID/x_user_id/token_store_key、Token SHA-256/权限一致。
- `drama_admin_user` 唯一匹配证据成立，正确 owner 可见；其他租户不可见。
- 重启后 owner、快照和 disconnected 历史保持；为 disconnected 行人为放置的 live Token/`.*.disconnecting` 会在启动时删除。

## V1 生产部署记录（2026-07-14）

- V1 GitHub 精确提交：`eccabcb0d49714efa90403b140c0d2f77e5182dc`。
- 发布目录：`/root/releases/ai-x-account-authorization-eccabcb0d497`。
- V1 部署前备份：`/root/backups/drama_material_service/20260714T041337Z-x-accounts-eccabcb`。
- V1 两服务 active，Nginx、Cookie/API Token 边界、callback 日志探针和文件权限验证通过；服务器测试 16/16。
- 用户提供的生产页面截图表明当前已有 1 条授权账号。V2 部署前仍必须从 live SQLite/Token 目录重新只读确认数量和 hash，不能仅以截图代替迁移基线。

## V2 本地与生产部署记录（2026-07-14）

- `python scripts/test_x_accounts.py`：28/28 通过。
- `python scripts/test_x_accounts_app_contract.py`：5/5 通过。
- Backfill 专项自动化：4/4 通过。
- 全量 `py_compile`、QuickNav/两个页面内联 JS、navigation JSON、`git diff --check` 和 Playwright 三路径均通过，浏览器 console 0 error。
- V2 GitHub 精确提交：`e00bd30adb466f92b38f218bfb7f288ea7ff0a69`。
- 服务器精确 release：`/root/releases/ai-x-account-authorization-e00bd30adb466`。
- V2 部署前备份：`/root/backups/drama_material_service/20260714T070906Z-x-accounts-v2-e00bd30`；备份包含 live 代码/静态、Nginx、systemd、两个 env、SQLite 在线备份及 Token 目录，敏感文件保持 root-only。
- 备份中的 `source-live-manifest.sha256` 保留部署前 live 路径/hash 基线；`manifest.sha256` 使用备份目录相对路径并覆盖全部备份文件，现场 `sha256sum -c manifest.sha256` 全部通过。备份目录为 `0700`，两个清单、SQLite 与 Token 文件均为 `0600`。
- 在生产数据副本先执行 `ensure_storage()`，随后 backfill 严格 dry-run 得到 `legacy=1, resolved=1, unresolved=0, updated=0`，apply 得到 `legacy=1, resolved=1, unresolved=0, updated=1`。副本演练通过后才进入 live 迁移。
- Live sidecar 停写期间执行相同 additive migration 与 backfill：严格 dry-run 为 1 条可解析、0 条未解析，apply 更新 1 条。旧记录的 row ID、X user ID 与 `active` 状态保持不变，owner 回填到唯一匹配的非空 tenant/user；真实账号标识不写入 Git。
- Token SHA-256 为 `cc6040d3f8e20a00561785f18209858ee3f89a5dd058a707cc66d9dea5888a6f`，文件权限 `0600`；该 hash/权限在副本演练、live migration、sidecar 启动及真实 `/2/users/me` 同步后均保持不变。Sidecar SQLite 权限为 `0600`。
- `x-post-automation.service` 与 `drama-material-api.service` 部署后均为 active。该机器的 Nginx 不是 systemd unit，配置校验通过后使用 `nginx -s reload` 完成重载。
- `/x-oauth/health`、`/x-accounts.html`、`/x-account-list.html` 均返回 200；未登录访问 owner/admin API 均返回 401，且响应带 `Cache-Control: no-store`。
- 生产模块级隔离探针结果：回填 owner 的 `mine=1`，admin `all=1`，其他 tenant/user `mine=0`；其他 owner 对现有记录执行 verify/logout 均返回 404，Token hash 不变。Live/release、公网页面副本与 Nginx 配置的部署文件 hash 一致。
- 2026-07-14T07:17:10Z 使用现有凭证真实调用 `/2/users/me` 同步成功，账号保持 `active`；public metrics 快照为 followers/following/tweet/listed/like/media=`0/1/2/0/0/0`，Token hash 未变化。
- 部署窗口内 sidecar/main journal 对 access/refresh token、Client Secret、state/code 等敏感字段的命中为 0；Nginx 最近 500 条中带查询串的 `/x-oauth/callback?` 固定字符串命中为 0。未执行真实 callback/revoke，因此该结论只覆盖本次实际流量。
- 为避免取消现有生产账号，本次未执行真实 logout/revoke；也未执行真实跨 owner OAuth callback 或带真实 Feishu Cookie 的生产浏览器验收。页面三路径仅在本地 Playwright mock 环境通过，不能据此宣称上述生产链路已验收。

## V2 回滚方案

1. 在任何服务恢复前停止 sidecar 与主 API 的 V2 写入，保留失败现场和日志。
2. 恢复 V2 部署前备份中的 `app.py`、feature、两个页面/导航、systemd、Nginx、env、SQLite 和 Token 目录；验证绝对路径均在目标项目/数据目录内。
3. 检出上一个 GitHub 精确 commit，执行 Python/Nginx/systemd 检查，依次启动 sidecar、主 API并 reload Nginx。
4. 恢复后对原 legacy 账号做 Token/owner/列表验证，检查 internal 公网 404和 callback 日志。
5. 如果 V2 期间进入过 `revoke_pending` 或成功调用任一远端 revoke，旧备份 Token 在 X 侧可能已部分/全部不可用；不得把该 Token 恢复成 active。优先重试完成退出，或隔离旧凭证并要求 owner 重新授权。
6. V2 新增列是 additive，回滚到 V1 时可保留不用；不要为回滚执行破坏性 drop column。只有从完整备份恢复时才回退 DB 文件。

## 注意事项

- 不整体覆盖生产复合单体；以 live `app.py` 做三方比对并保存 rollback point。
- Revoke 是外部不可逆动作，生产真实 logout 必须由用户确认，不能作为无人值守部署 smoke test。
- Legacy owner 未唯一匹配时宁可 admin-only，也不能按 user_id 便利性放开。
- V2 已部署，生产迁移/Token 保全、服务/API smoke、模块级 owner/admin 查询与真实 `/2/users/me` 同步通过；结论仅限这些已执行项目。真实 Cookie 浏览器、真实跨 owner OAuth 与真实 logout/revoke 完成前，不得标记“全量生产验收通过”。
