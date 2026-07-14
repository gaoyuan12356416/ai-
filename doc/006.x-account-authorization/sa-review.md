# SA 评审意见

## 结论

V1 架构继续采用独立 X OAuth sidecar，凭证只由 sidecar 持有。V2 页面拆分、联合 owner 隔离、`revoke_pending` 可恢复退出状态机，以及 owner pending 门禁/owner→account 锁序均已落地并部署。Sidecar 28/28、App contract 5/5、backfill 4/4、全量编译/静态检查及本地 Playwright 三路径（console 0 error）通过；生产副本/live migration、Token 保全、服务/API smoke、模块级隔离与真实 `/2/users/me` 同步通过。真实 Cookie 浏览器、真实跨 owner OAuth 和真实 X revoke 仍待验收。

## 问题清单

| 编号 | 严重级别 | 位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| SA-001 | 高 | OAuth callback | 直接把 callback 迁入主后台会重复保存 Client Secret 并与现有路由冲突 | 保留 `/x-oauth/callback` sidecar 所有权 | 已采纳（V1） |
| SA-002 | 高 | Nginx | `/x-oauth/` 宽泛代理会暴露 internal API | callback/health 精确 location，其余返回 404 | 已采纳（V1） |
| SA-003 | 高 | Token 存储 | 主业务 SQLite 不能存 X Token | Token 独立文件 `0600`，sidecar DB `0600` 仅存元数据 | 已采纳（V1） |
| SA-004 | 中 | 权限 | 普通 API Token 可能继承模块权限 | X API 统一要求 Feishu Cookie；admin API 额外要求 admin | App 5/5、本地 Playwright 及生产未登录 401/no-store 通过；真实 Cookie 待回归 |
| SA-005 | 中 | 状态 | Access Token 到期直接显示授权失效会误导 | 有 Refresh Token 时显示 `refresh_required` | 已采纳（V1） |
| SA-006 | 中 | 导航 | 只改 quick-nav 默认值会被线上 navigation.json 覆盖 | 同步 quick-nav、navigation.json 和公网副本 | 已采纳（V1） |
| SA-007 | 高 | Callback 日志 | 默认 request line 会记录 code/state | callback 关闭 access log，sidecar 仅记 path | 已采纳（V1） |
| SA-008 | 中 | POST 安全 | Cookie 写操作缺少显式同源约束 | 强制 JSON 并校验 Origin/Referer | 已采纳（V1） |
| SA-009 | 中 | 刷新并发 | 并发 Refresh Token 轮换可能互相作废 | sidecar 按 `x_user_id` 串行 callback/verify/logout | V2 本地实现/并发回归通过 |
| SA-010 | 中 | API 契约 | 错误正文透传、时间和缓存未锁定 | 白名单错误、UTC Z；主 API、Nginx 与页面 fetch 三层 no-store，尤其覆盖 admin 列表 | App contract 5/5、浏览器通过 |
| SA-011 | P0 | 个人列表/校验 | V1 返回全局列表且 verify 只按记录 ID，可形成跨用户 IDOR | actor 传入 sidecar；SQL 同时过滤 owner tenant/user；admin 使用独立 API | BUG-004 已修复；生产模块级 mine=1/all=1/other=0，真实 Cookie IDOR 待验收 |
| SA-012 | P0 | OAuth callback upsert | V1 `ON CONFLICT(x_user_id)` 会覆盖其他后台用户的 Token 和授权人 | `x_user_id` 全局唯一但归属不可转移；跨 owner 返回 `x_account_owned_by_other`，原数据不变 | BUG-005 自动化通过；真实跨 owner OAuth 待验收 |
| SA-013 | 高 | 租户边界 | 只按 Feishu `user_id` 隔离可能在多租户下串权 | owner 主键语义固定为 `tenant_key + user_id`，禁止任一字段为空时落入普通 owner 查询 | 本地实现/复核通过 |
| SA-014 | 高 | Admin 页面/API | 仅靠前端隐藏无法保护全量列表 | 静态页面非 admin 显示无数据权限门；`/api/admin/x-accounts*` 服务端 Cookie admin；导航 key `xAccountList` admin-only | App contract 5/5、Playwright 通过 |
| SA-015 | 高 | 退出授权 | 失败时若仍投影 active、允许新 OAuth 或提前删凭证会产生不可恢复/孤儿 Token | 前置 pending；Access 先、Refresh 最后；失败保留 Token、禁 verify/authorize；stale callback 换 Token 前拒绝；统一 owner→account 锁序；成功后才删凭证并断开 | Sidecar 28/28 通过；真实 revoke 待验收 |
| SA-016 | 高 | Legacy 迁移 | 添加 owner 列可能使生产记录不可见或丢 Token | 标准脚本默认 dry-run；仅唯一匹配回填；`--require-all-resolved` 阻断；apply 前后断言 Token hash | Backfill 4/4；副本/live 各回填 1 条，row 与 Token hash/权限不变 |
| SA-017 | 中 | 账号资料 | 列表逐账号请求 X 会放大成本，超长 username 可能生成不安全链接 | callback/verify 保存快照；主页链接只接受 `[A-Za-z0-9_]{1,50}` | Sidecar/Playwright 及生产真实 `/users/me` 同步通过 |
| SA-018 | 高 | 启动恢复 | disconnected 行旁残留 live Token/tombstone 可能跨重启保留 | 启动按账号锁删除 live Token 与旧 tombstone | Sidecar 28/28；生产 active 账号启动保全通过，disconnected 残留分支未实测 |
| SA-019 | 中 | 时间可观测性 | 缺少刷新、同步、更新、退出时间 | 两页显示相应时间列 | Playwright 三路径通过 |

## 决策记录

- Callback 保持 `https://ai.yingliangads.com/x-oauth/callback`，sidecar 仍为 Token 唯一持有方。
- `/x-accounts.html` 是个人授权页；管理员全量页固定为 `/x-account-list.html`，导航 key 为 `xAccountList`。
- admin 同步接口固定为 `POST /api/admin/x-accounts/{id}/verify`；admin 本期不代 owner logout。
- owner 由 `tenant_key + user_id` 唯一识别；admin 角色是查看全量的独立授权，不会改变记录 owner。
- 同一 X 账号只能归属一个 owner；跨 owner 重授权拒绝，不隐式转移。
- 退出授权开始远端调用前持久化 `revoke_pending`；Access Token 先撤销、Refresh Token 最后撤销。任一步失败保留 live Token、禁止 verify、允许重试 logout；只有全部成功并完成本地清理才进入 `disconnected`。
- Owner 任一账号 pending 时禁止新 authorize；stale callback 在 token request 前拒绝。Authorize/callback/logout 使用 owner→account 锁序，callback 写入新 Token 后 logout 才撤销该新凭证。
- Sidecar 启动清理所有 `disconnected` 行对应的残留 live Token 与旧 `.disconnecting` tombstone。
- Legacy 回填只接受 `drama_admin_user` 唯一匹配，不做跨租户 user_id 兜底；标准入口是默认 dry-run 的 `scripts/backfill_x_account_owners.py`，生产使用 `--require-all-resolved` 作门槛。
- Admin 列表 API、Nginx 与页面 fetch 都禁止缓存；两个页面展示刷新、同步、更新及退出时间。

## PM 修订确认

SA-011 至 SA-019 已写入 V2 requirements、API、测试与部署文档。V2 精确提交已部署；生产迁移/Token 保全、服务/API smoke、模块级查询和真实资料同步已完成。真实 Cookie 浏览器、真实跨 owner OAuth 与真实 X revoke 尚未完成，故结论不升级为全量生产验收通过。
