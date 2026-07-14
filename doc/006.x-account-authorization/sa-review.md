# SA 评审意见

## 结论

V1 独立 X OAuth sidecar、V2 页面拆分和联合 owner 隔离继续保留。V3 按用户明确决策把“退出授权”改为本地软停用：账号在账号锁内写为 `disabled`，Token 原样保留，不调用 X revoke；`disabled` 不可校验、不可发布，同一 owner 重新授权后才能恢复 `active`。旧远端 revoke/`revoke_pending` 状态机及 owner pending 门禁已被 V3 取代，不再以真实 revoke 作为验收目标。V3 Sidecar 28/28、App contract 5/5 通过；生产软停用浏览器流程仍待验收。

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
| SA-015 | 高 | 退出/停用语义 | V2 远端 revoke 依赖 X 上游且与用户“仅停止后台使用”的最终需求不一致 | V3 `/logout` 仅在本地写 `disabled`，不读取/删除 Token、不调用 X；禁 verify/发布，重新授权后恢复 | V3 已取代旧 revoke/pending 状态机；Sidecar 28/28 通过 |
| SA-016 | 高 | Legacy 迁移 | 添加 owner 列可能使生产记录不可见或丢 Token | 标准脚本默认 dry-run；仅唯一匹配回填；`--require-all-resolved` 阻断；apply 前后断言 Token hash | Backfill 4/4；副本/live 各回填 1 条，row 与 Token hash/权限不变 |
| SA-017 | 中 | 账号资料 | 列表逐账号请求 X 会放大成本，超长 username 可能生成不安全链接 | callback/verify 保存快照；主页链接只接受 `[A-Za-z0-9_]{1,50}` | Sidecar/Playwright 及生产真实 `/users/me` 同步通过 |
| SA-018 | 高 | 启动恢复 | legacy `disconnected` 行旁残留 live Token/tombstone 可能跨重启保留；若把 `disabled` 混入清理会违背 Token 保留决策 | 启动清理只处理 legacy `disconnected`；`disabled` 永不删除 Token | legacy 清理逻辑保留，V3 disabled Token 保全回归通过 |
| SA-019 | 中 | 时间可观测性 | 缺少刷新、同步、更新、停用时间 | 两页显示相应时间列；`disabled` 记录沿用兼容字段 `disconnected_at` 表示本地停用时间 | V3 页面契约已统一，生产页面待验收 |
| SA-020 | P0 | 未来 X 发布 | 先查 `active`、释放锁、再读取 Token/调用 X，会允许停用与发布之间发生 TOCTOU 竞争 | 所有发布代码必须在 `publish_credentials` 同一上下文内完成；上下文持锁重查精确 `active`，敏感凭证只 yield `access_token`，不暴露完整 Token/Refresh Token | V3 helper 与并发回归通过；实际发布尚未接入 |
| SA-021 | 高 | Legacy 状态收敛 | 现有 `revoke_pending` 若继续要求远端重试会永久阻塞；`disconnected` 若批量改成 disabled 可能错误恢复已删除凭证 | 旧 pending 可无 Token/无上游调用直接收敛为 `disabled`；legacy `disconnected` 保持原状态和清理语义 | V3 回归通过 |

## 决策记录

- Callback 保持 `https://ai.yingliangads.com/x-oauth/callback`，sidecar 仍为 Token 唯一持有方。
- `/x-accounts.html` 是个人授权页；管理员全量页固定为 `/x-account-list.html`，导航 key 为 `xAccountList`。
- admin 同步接口固定为 `POST /api/admin/x-accounts/{id}/verify`；admin 本期不代 owner logout。
- owner 由 `tenant_key + user_id` 唯一识别；admin 角色是查看全量的独立授权，不会改变记录 owner。
- 同一 X 账号只能归属一个 owner；跨 owner 重授权拒绝，不隐式转移。
- V3 `/api/x-accounts/{id}/logout` 路径为兼容保留，但业务语义固定为本地软停用：账号锁内写 `disabled`、清空旧错误并记录停用人/时间；不得读取、删除或改写 Token，不得调用 X revoke。
- `disabled` 是本地后台终态：列表必须保持该状态，不因 Token 尚在而重新投影为 active；owner/admin verify 均 fail closed；只有重新授权同一账号才恢复 `active`。
- owner 的 legacy `revoke_pending` 不再阻塞 authorize/callback，可通过同一 logout 路径收敛为 `disabled`。旧远端 Access-first/Refresh-last revoke 状态机仅保留为 V2 历史，不再执行或验收。
- Future X publish 必须在 `publish_credentials(account_id, actor, scope)` 同一上下文中完成上游请求。该上下文在账号锁内重查 owner/scope/status，只接受精确 `active` 和非空 Access Token，敏感凭证只 yield `access_token` 字符串（另有脱敏 account item）；调用方不得把该字符串带出上下文、返回 API、写日志或绕过 helper 直读完整 Token 文件。实际发布当前尚未接入。
- Sidecar 启动只清理 legacy `disconnected` 行对应的残留 live Token 与旧 `.disconnecting` tombstone；不得清理 `disabled` Token。legacy `disconnected` 不自动迁移为 `disabled`。
- Legacy 回填只接受 `drama_admin_user` 唯一匹配，不做跨租户 user_id 兜底；标准入口是默认 dry-run 的 `scripts/backfill_x_account_owners.py`，生产使用 `--require-all-resolved` 作门槛。
- Admin 列表 API、Nginx 与页面 fetch 都禁止缓存；两个页面展示刷新、同步、更新及退出时间。

## PM 修订确认

SA-011 至 SA-019 的 V2 历史结论继续保留；SA-015 已由 V3 用户决策重定义，新增 SA-020/021 锁定发布并发门禁和 legacy 收敛。V3 设计与代码级评审通过，Sidecar 28/28、App 5/5 通过；真实 Cookie 浏览器、真实跨 owner OAuth 与生产软停用页面仍待验收。真实 X revoke 不再属于当前方案或验收范围。
