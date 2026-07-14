# SA 代码评审

## 结论

V1 两轮评审问题均已修复。V2 本地代码评审与验证通过：Sidecar 28/28、App 5/5、backfill 4/4、全量编译/静态及 Playwright 三路径通过，console 0 error。生产迁移、真实 Cookie 生产验收与真实 X revoke 未执行。

## 评审范围

- `app.py`：Cookie/module/admin 鉴权、actor 生成、owner/admin 路由、同源约束、错误白名单与审计。
- `features/x_accounts/client.py`：owner/admin actor 传递与 loopback internal contract。
- `features/x_accounts/oauth_service.py`：schema migration、owner SQL 边界、callback upsert、防覆盖、资料快照、pending/revoke 状态机、启动残留清理、并发与日志。
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
| CR-014 | P1 | admin API/page | 若复用 owner 模块权限或前端隐藏，非 admin 仍可能访问全量 | `/api/admin/x-accounts*` 必须 Cookie admin；admin 页面不调用 owner API | 实现已复核；最新静态/浏览器套件待验收 |
| CR-015 | P1 | logout/revoke | 失败时若保持 active、允许 verify 或提前删 Token，会把半完成远端撤销误当作正常授权 | 同账号锁；远端前写 `revoke_pending`；Access 先、Refresh 最后；失败保留 live Token、禁 verify、允许 logout 重试；全部成功后才删凭证并置 disconnected | 实现已复核；最新失败/重试套件与真实 revoke 待验收 |
| CR-016 | P1 | owner backfill | owner 默认空后若按 user_id 兜底会跨租户认领；人工 SQL 易误写 | 标准脚本默认 dry-run，只回填唯一非空 tenant 匹配；`--require-all-resolved` 阻断；apply 使用事务与 guarded update | Backfill 4/4 通过；生产回填待执行 |
| CR-017 | P2 | profile DTO/UI | X 用户主页必须安全构造，optional 字段可能缺失 | 链接仅接受 `[A-Za-z0-9_]{1,50}`；所有可选字段空值安全 | Sidecar/Playwright 通过 |
| CR-018 | P1 | startup cleanup | disconnected 行对应的 live Token/旧 tombstone 可能在异常中断后残留 | sidecar 启动完成 storage 初始化后按账号锁清理 live Token 与 `.*.disconnecting` | Sidecar 28/28 通过 |
| CR-019 | P2 | owner/admin UI | 缺少刷新、同步、更新和退出时间会降低状态可诊断性 | 个人页显示 refresh/verify、updated/disconnected；admin 页显示 expiry/refresh、verify/profile sync、updated/disconnected | 实现已复核；登录态页面待验收 |

## V2 代码门槛与当前结果

- BUG-004/005 实际修复 diff 与本地完整回归通过。
- Owner service 接收 actor；列表/verify/logout 的 owner 过滤在 sidecar 执行，不依赖 UI。
- Admin query/verify 与 owner query/verify 走独立主 API 权限门；admin verify 是明确的“同步资料/状态”能力，不改变 owner。
- Callback 在 Token 文件写入前完成跨 owner 判断，冲突测试逐字节确认旧 Token 不变。
- Logout 状态机与 startup cleanup 已由 Sidecar 28/28 验证。
- Backfill CLI 符合默认 dry-run、唯一匹配、`--require-all-resolved` 和 guarded apply 契约；自动化与生产 `drama_admin_user` 回填/Token hash 断言仍待执行。
- Admin API、Nginx 与页面 fetch 的 no-store 以及两页时间列、username 50 字符上限已完成静态契约复核；完整静态/浏览器验证待执行。

## 编译 / 验证结果

V1 历史证据：

- `python scripts/test_x_accounts.py`：16/16 通过。
- `python -m py_compile`：主 app、sidecar/client及规定回归模块通过。
- `node --check`、navigation JSON、`git diff --check`：通过。

V2 本地证据：

- `python scripts/test_x_accounts.py`：28/28 通过。
- `python scripts/test_x_accounts_app_contract.py`：5/5 通过。
- backfill 4/4、全量 `py_compile`、QuickNav/两个页面内联 JS、navigation JSON、`git diff --check` 通过。
- Playwright 管理员个人页、管理员全量页、普通用户权限门三路径通过，console 0 error。
- 生产部署、真实 Cookie/浏览器、真实 X revoke 和 legacy owner 实库回填待执行。
