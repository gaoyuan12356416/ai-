# SA 代码评审

## 结论

V1/V2 历史评审问题继续保持修复。V3 本地软停用代码评审通过：`/logout` 不再调用 X revoke，也不读取、删除或改写 Token；账号在锁内写为 `disabled`，verify 和发布均 fail closed，旧 `revoke_pending` 可收敛，legacy `disconnected` 保持原语义。实际 X Post 发布尚未接入；未来发布必须在持账号锁的 `publish_credentials` 上下文内完成。V3 Sidecar 28/28、App 5/5 通过；生产软停用浏览器流程尚未执行，真实 X revoke 已退出验收范围。

## 评审范围

- `app.py`：Cookie/module/admin 鉴权、actor 生成、owner/admin 路由、同源约束、错误白名单与审计。
- `features/x_accounts/client.py`：owner/admin actor 传递与 loopback internal contract。
- `features/x_accounts/oauth_service.py`：schema migration、owner SQL 边界、callback upsert、防覆盖、资料快照、V3 soft disable、`publish_credentials`、legacy 状态收敛、启动残留清理、并发与日志。
- `scripts/backfill_x_account_owners.py`：默认 dry-run、唯一匹配、严格解析门槛、事务与并发保护。
- 两个页面、导航、Nginx、systemd、配置模板。
- 需求、API、测试、部署、缺陷与测试报告。

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-001 | P0 | Nginx | 精确列表未代理，callback 会记录 code/state | exact location + callback `access_log off` | 已修复（V1） |
| CR-002 | P1 | app/client | 上游错误正文直出且状态码丢失 | 白名单 code/status/固定文案 | 已修复（V1） |
| CR-003 | P1 | app | Cookie POST 缺少显式同源约束 | 强制 JSON 并校验 Origin/Referer | 已修复（V1） |
| CR-004 | P1 | sidecar | callback/verify 未共享账号锁 | 统一按 `x_user_id` 串行 | Sidecar 28/28 通过 |
| CR-005 | P1 | sidecar | `/users/me` 未核对 Token 属主 | 非空且严格匹配 `x_user_id` | 已修复（V1） |
| CR-006 | P1 | sidecar | 环境变量可删减必需 scope | 固定 `REQUIRED_SCOPES` 并 fail closed | 已修复（V1） |
| CR-007 | P2 | urllib | 30x 可能转发 Authorization | token/user/revoke 全部禁自动跳转 | Sidecar 28/28 通过 |
| CR-008 | P2 | sidecar | 审计失败可能反转已成功结果 | 审计 best-effort，核心结果独立 | 已修复（V1） |
| CR-009 | P2 | UI/API | UTC、缓存、缺失 scope 展示不足 | UTC Z；主 API、Nginx、页面 fetch 三层 no-store；缺失标签 | App 5/5、Playwright 通过 |
| CR-010 | P0 | `list_accounts` / owner GET | V1 `SELECT *` 返回全局账号，个人页会跨用户泄漏 | actor tenant/user 下沉到 sidecar，owner SQL 联合过滤；admin 独立 query | 本地回归通过 |
| CR-011 | P0 | `verify_account(account_id)` | 只按整数 ID 查找/更新，任何有模块权限用户可校验他人账号 | owner verify 在锁前后都按 `id+tenant+user` 重查；越权统一 404 | 本地回归通过 |
| CR-012 | P0 | callback upsert | `ON CONFLICT(x_user_id)` 无 owner 条件，会覆盖其他 owner 的 Token/授权人 | 锁内先读 owner；跨 owner 在写 Token 前拒绝，比较旧 Token hash/DB 不变 | 本地回归通过 |
| CR-013 | P0 | state/actor | V1 state 未保存 tenant，callback 无法恢复联合 owner | schema additive 增 actor tenant；主 API 拒绝 owner tenant/user 缺失 | App 5/5 通过 |
| CR-014 | P1 | admin API/page | 若复用 owner 模块权限或前端隐藏，非 admin 仍可能访问全量 | `/api/admin/x-accounts*` 必须 Cookie admin；admin 页面不调用 owner API | 实现/App/本地 Playwright 通过；生产未登录 401/no-store，通过真实 Cookie 待验收 |
| CR-015 | P1 | logout/disable | V2 远端 revoke 路径不再符合用户最终选择，继续保留会造成上游失败或误删 Token | 删除当前 revoke 调用；账号锁内仅 guarded update 为 `disabled`，保留 Token，清空旧错误，重复调用幂等 | V3 已修复；软停用/Token 字节保全回归通过 |
| CR-016 | P1 | owner backfill | owner 默认空后若按 user_id 兜底会跨租户认领；人工 SQL 易误写 | 标准脚本默认 dry-run，只回填唯一非空 tenant 匹配；`--require-all-resolved` 阻断；apply 使用事务与 guarded update | Backfill 4/4；生产副本/live dry-run/apply 均通过，更新 1 条 |
| CR-017 | P2 | profile DTO/UI | X 用户主页必须安全构造，optional 字段可能缺失 | 链接仅接受 `[A-Za-z0-9_]{1,50}`；所有可选字段空值安全 | Sidecar/Playwright 通过 |
| CR-018 | P1 | startup cleanup | legacy `disconnected` 残留 Token 仍需清理，但误清理 `disabled` 会违反 V3 Token 保留契约 | cleanup 查询必须继续精确限定 `status='disconnected'`，不得包含 disabled | legacy 逻辑保留；disabled Token 保全回归通过 |
| CR-019 | P2 | owner/admin UI | 若仍显示“解除授权/删除 Token”，会与 V3 本地停用语义冲突 | 两页显示 `disabled=已停用`，明确 Token 保留、不调用 X、只有 active 可发布；保留 legacy 状态区分 | V3 页面已修订；生产登录态页面待验收 |
| CR-020 | P0 | publish credentials | 发布方若在状态检查后释放锁，再读 Token/发帖，会与 logout 形成 TOCTOU，停用后仍可能发布 | `publish_credentials` 在账号锁内二次查行，只接受精确 `active` 和非空 Access Token；敏感凭证只 yield `access_token`，上游发布必须在同一 context 内完成 | helper、状态拒绝和锁保持并发回归通过；实际发布尚未接入 |
| CR-021 | P1 | legacy/reauthorize | `revoke_pending` 可能卡死；把 `disconnected` 当软停用或继续 owner pending 门禁会阻止正常恢复 | pending 可直接软停用；disabled 可由同 owner 重新授权恢复；disconnected 保持历史终态；移除 pending authorize/callback 门禁 | V3 回归通过 |

## V3 代码门槛与当前结果

- BUG-004/005 的 V2 owner 防护继续生效；V3 不改变 owner/admin 鉴权边界。
- Owner service 接收 actor；列表/verify/logout 的 owner 过滤在 sidecar 执行，不依赖 UI。
- Admin query/verify 与 owner query/verify 走独立主 API 权限门；admin verify 是明确的“同步资料/状态”能力，不改变 owner。
- Callback 在 Token 文件写入前完成跨 owner 判断，冲突测试逐字节确认旧 Token 不变。
- 当前代码不再定义或调用 X `REVOKE_URL`/`revoke_token`；logout 在 owner 和账号锁边界内仅更新 `disabled`，不调用 `read_token_file`、`delete_token_artifacts` 或 X HTTP。
- `status_for`/DTO 保持 `disabled`，输出 `publish_eligible=false`；owner/admin verify 对 disabled 返回 `x_account_disabled`，不会刷新或调用 `/users/me`。
- `publish_credentials` 是未来发布唯一凭证入口：在账号锁内重查 scoped row/status，精确要求 `active` 与 Access Token，只 yield `access_token` 字符串而不暴露完整 Token/Refresh Token，并在 context 退出前持续持锁。实际发布尚未接入；未来发布实现不得在 context 外缓存或使用该字符串。
- legacy `revoke_pending` logout 可直接落为 `disabled` 并清除旧错误；legacy `disconnected` 幂等保持不变，startup cleanup 仍只处理 disconnected。disabled Token 不参与清理。
- owner pending authorize/callback 门禁已移除；同一 owner 重新授权 disabled 账号恢复 `active`，跨 owner 防覆盖仍保持。
- Backfill CLI 符合默认 dry-run、唯一匹配、`--require-all-resolved` 和 guarded apply 契约；生产副本/live 各解析并更新 1 条，原 row/x_user_id/status、Token SHA-256 与 `0600` 保持。
- Admin API、Nginx 与页面 fetch 的 no-store 以及两页时间列、username 50 字符上限已完成静态契约和本地 Playwright 复核；生产未登录 API no-store 通过，真实 Cookie 浏览器仍待执行。

## 编译 / 验证结果

V1 历史证据：

- `python scripts/test_x_accounts.py`：16/16 通过。
- `python -m py_compile`：主 app、sidecar/client及规定回归模块通过。
- `node --check`、navigation JSON、`git diff --check`：通过。

V2 历史本地证据：

- `python scripts/test_x_accounts.py`：28/28 通过。
- `python scripts/test_x_accounts_app_contract.py`：5/5 通过。
- backfill 4/4、全量 `py_compile`、QuickNav/两个页面内联 JS、navigation JSON、`git diff --check` 通过。
- Playwright 管理员个人页、管理员全量页、普通用户权限门三路径通过，console 0 error。
- 生产已部署；legacy owner 实库回填、Token 保全、服务/API smoke、模块级隔离和真实 `/2/users/me` 同步通过。

V3 当前本地证据：

- `python scripts/test_x_accounts.py`：28/28 通过；覆盖本地软停用幂等、无 Token/X 访问、Token 字节保全、legacy pending 收敛、legacy disconnected 重复 logout 清理、disabled 校验/发布拒绝、同 owner 重授权、延迟 callback 最后写入胜出和 publish context 锁保持。
- `python scripts/test_x_accounts_app_contract.py`：5/5 通过。
- V3 用例数少于 V2 28/28 是因为远端 revoke 顺序/失败/HTTP 兼容用例已随废弃实现删除，并由软停用和发布锁用例替代，不代表覆盖退化。
- 生产真实 Cookie/浏览器、真实跨 owner OAuth 与 V3 软停用页面流程仍待执行；真实 X revoke 不再执行。
