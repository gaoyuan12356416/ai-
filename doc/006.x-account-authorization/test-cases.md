# 测试用例

> 当前版本：V3（2026-07-14，本地软停用）

## 测试范围

- V1 回归：Cookie/module 鉴权、OAuth state/PKCE、Token 隔离/刷新、并发、日志脱敏、Nginx/internal 边界。
- V2 回归：个人页与 admin 页拆分、`tenant_key + user_id` owner 隔离、IDOR、防跨 owner 重授权覆盖、资料快照、admin 同步、legacy 回填 CLI 与回滚。
- V3 新增：`status=disabled` 本地软停用、Token 不读/不删/不远端撤销、legacy `revoke_pending` 转停用、重新授权恢复、严格 active 发布凭证 guard、发布/停用锁并发以及 legacy `disconnected` 清理兼容。

## 测试数据

- Owner A：`tenant=t1,user=u1`；Owner B：`tenant=t1,user=u2`；Owner C：`tenant=t2,user=u1`，用于验证同 user ID 跨租户隔离。
- Admin：独立 admin Cookie；普通用户：有/无 `x_accounts` 权限各一组；API Token 一组。
- Mock X-A、X-B 两个不同 `x_user_id`，以及同一个 X-A 被不同 owner 重新授权的冲突数据。
- Mock `/2/users/me` 完整/缺失可选字段、public_metrics 变化、Token refresh；另准备 active/disabled/revoke_pending/disconnected 状态、可读/不可读 Token、缺 Access Token、启动残留与发布锁并发数据。
- V1 生产副本：1 条 `x_authorized_account`、对应 Token 文件和旧 schema，用非敏感 hash 验证迁移前后不变。
- Legacy 回填副本：唯一/零/多条 `drama_admin_user` 匹配，覆盖默认 dry-run、`--apply`、`--require-all-resolved` 与并发条件更新。
- 真实生产 X 账号只用于最终 callback、资料同步、本地软停用与重新授权验收；不执行 X revoke，测试日志不得输出 Token。

## 用例列表

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| TC-001 | 未登录访问个人列表 | 无 Cookie | GET `/api/x-accounts` | 401 | P0 | V2 生产 owner/admin API 均 401 且 no-store |
| TC-002 | 无模块权限 | 普通用户 | GET/POST owner API | 403 | P0 | App 5/5 与代码复核通过；生产待回归 |
| TC-003 | API Token 尝试读取/授权 | 有 API Token | GET list、POST authorize | 403 `cookie_auth_required` | P0 | V1 生产历史通过；V2 App/Sidecar 本地通过，生产待回归 |
| TC-004 | 发起授权 | 有权限 Cookie | POST authorize | URL 含五项 scope、S256、正确 callback；state 保存 tenant/user | P0 | V3 Sidecar 28/28 通过 |
| TC-005 | state 错误/过期/重放 | 构造异常 callback | 访问 callback | 拒绝且不写 Token | P0 | V3 Sidecar 28/28 通过 |
| TC-006 | 首次授权与资料快照 | Mock token/user 成功 | callback | owner 正确、新增 1 行、Token 0600、完整资料快照 | P0 | V3 Sidecar 28/28 通过 |
| TC-007 | 同 owner 重复授权 | 同一 owner + x_user_id | 再次 callback | 仍 1 行，owner/首次授权不变，Token/最近授权/快照更新 | P0 | V3 Sidecar 28/28 通过 |
| TC-008 | 同 owner 多账号 | X-A、X-B | 两次 callback | 个人列表 2 行且均属当前 owner | P0 | V3 Sidecar 28/28 通过 |
| TC-009 | scope 缺失 | Token 少一项 scope | callback | `scope_missing` 并列出缺失权限 | P0 | V3 Sidecar 28/28 通过 |
| TC-010 | Access Token 到期 | 有 Refresh Token | 个人/admin 列表 | `refresh_required`，列表不请求 X | P1 | V3 Sidecar 28/28 通过 |
| TC-011 | 本人主动校验/刷新 | Token 过期 | POST owner verify | 刷新/轮换 Token、状态 active、资料快照更新 | P0 | V3 Sidecar 28/28 通过 |
| TC-012 | 上游已撤销 | refresh `invalid_grant` | POST verify | 状态 revoked，错误脱敏 | P0 | V3 Sidecar 28/28 通过 |
| TC-013 | Internal 接口鉴权 | 无/错 internal token | 访问 `/internal/*` | 403 | P0 | V3 Sidecar 28/28 通过 |
| TC-014 | 敏感数据泄漏扫描 | 完成授权/校验/软停用 | 搜索 API/HTML/log/audit | 无 Secret/Token/code/state/verifier；软停用不产生任何 X 上游请求 | P0 | V3 Sidecar 28/28 通过；部署窗口 journal 敏感字段 0、Nginx callback 查询串 0，真实 callback 仍待验收 |
| TC-015 | 页面与导航拆分 | 各角色 Cookie | 打开两个页面/导航 | 个人入口按模块权限；`xAccountList` 仅 admin；非 admin 打开静态 admin 页只见权限门且不请求数据 | P0 | JS/JSON 与 Playwright 三路径通过；生产 Cookie 待回归 |
| TC-016 | 服务重启恢复 | 多 owner + disabled/disconnected 数据 | 重启两个服务 | owner/快照/状态不丢；disabled Token 原字节保留，只有 legacy disconnected 凭证残留被清理 | P1 | V3 Sidecar 28/28 通过；生产 legacy disconnected 残留分支待验收 |
| TC-017 | Token 属主不一致 | `/users/me` 空或错误 ID | verify | `x_identity_mismatch`，不标 active | P0 | V3 Sidecar 28/28 通过 |
| TC-018 | 必需 scope 配置被删减 | env 缺 `media.write` | 启动/发起授权 | fail closed | P0 | V3 Sidecar 28/28 通过 |
| TC-019 | 并发 verify/callback/publish/logout | 并发请求 | 执行操作 | 同 `x_user_id` 串行；发布上下文持锁时停用等待，无旧 Token 覆盖新 Token | P0 | V3 Sidecar 28/28 并发回归通过 |
| TC-020 | 上游跨域 30x | 目标收集 Header | token/user 请求 | 拒绝跳转，不转发 Authorization | P1 | V3 Sidecar 28/28 通过 |
| TC-021 | Owner A 个人列表隔离 | A/B/C 各有账号 | A GET `/api/x-accounts` | 只返回 `t1+u1`，不返回 B/C | P0 | BUG-004/自动化通过；生产模块级 mine=1/all=1/other=0，真实 Cookie 待验收 |
| TC-022 | 同 user ID 跨 tenant 隔离 | A=`t1+u1`、C=`t2+u1` | 分别 GET 个人列表 | 两者列表互不重叠 | P0 | V3 Sidecar 28/28 通过；生产 Cookie 待回归 |
| TC-023 | 跨 owner verify IDOR | A 知道 B 的记录 ID | A POST `/{id}/verify` | 404；不调用 X，不改变 B 记录/Token | P0 | BUG-004/Sidecar 通过；生产模块级其他 owner 返回 404 且 Token hash 不变 |
| TC-024 | 跨 owner logout IDOR | A 知道 B 的记录 ID | A POST `/{id}/logout` | 404；不读取 Token、不调用 X、不改变 B 数据 | P0 | BUG-004/V3 Sidecar 通过；生产模块级其他 owner 返回 404 且 Token hash 不变 |
| TC-025 | 非 admin 全量 API | 普通有模块权限用户 | GET `/api/admin/x-accounts`、POST admin verify | 403，响应不泄漏总数/记录 | P0 | App 5/5 通过；生产待回归 |
| TC-026 | Admin 全量列表 | admin Cookie | GET `/api/admin/x-accounts` | 返回所有 owner/账号快照，含 owner tenant/user 和状态 | P0 | Sidecar/Playwright 与生产模块级 all=1 通过；真实 admin Cookie 待验收 |
| TC-027 | Admin 同步 | admin + 任意 owner 账号 | POST `/api/admin/x-accounts/{id}/verify` | 刷新状态/资料并审计，owner 不变 | P0 | Sidecar 通过；生产模块级真实 `/users/me` 同步成功，真实 admin Cookie 待验收 |
| TC-028 | 跨 owner 重授权 | X-A 已属 A，B callback X-A | 完成 token/user response | 409 `x_account_owned_by_other`；A 的 owner、Token hash、时间/快照不变 | P0 | BUG-005 修复；V3 Sidecar 28/28 通过 |
| TC-029 | owner 空 legacy 不可认领 | legacy row owner tenant 空 | 同 user_id 普通用户 GET/verify | 列表不可见、操作 404；仅 admin 可见 | P0 | V3 Sidecar 28/28 通过 |
| TC-030 | `/users/me` 字段快照 | 完整字段响应 | callback 后查 API/DB | 名称、头像、location、verified/protected、metrics、X 建档/同步时间正确 | P1 | Sidecar 通过；生产 2026-07-14T07:17:10Z 真实同步成功 |
| TC-031 | 可选资料缺失 | location/metrics 缺失 | callback/verify | 空值安全，页面不崩溃，不用错误账号值覆盖有效快照 | P1 | V3 Sidecar 28/28 与 V2 Playwright 通过；V3 页面待回归 |
| TC-032 | X 主页链接边界 | 16 字符、50 字符、51 字符及不安全字符 username | 打开 admin/owner 页面 | 1–50 位 `[A-Za-z0-9_]` 生成 `https://x.com/<username>`；超长/非法值不生成链接 | P1 | V3 Sidecar 28/28、V2 Playwright 通过；V3 页面待回归 |
| TC-033 | 本人软停用成功 | A 为 active，Token 文件存在 | POST owner `/logout` | 直接写 `status=disabled` 与停用审计；`publish_eligible=false`；不读 Token、不调用 X、不删除 Token、不清空到期元数据 | P0 | V3 Sidecar 28/28 通过；真实生产待验收 |
| TC-034 | Soft logout 幂等 | 账号已 disabled | 重复 POST owner `/logout` | 仍为 disabled；首次停用时间、Token 字节/权限与账号资料不变；只记录一次完成审计 | P0 | V3 Sidecar 28/28 通过 |
| TC-035 | Token 不可读也能停用 | active 或 revoke_pending，Token JSON 损坏 | POST owner `/logout` | 不解析 Token；成功写 disabled，损坏文件原字节保留，无 X 请求 | P0 | V3 Sidecar 28/28 通过 |
| TC-036 | Legacy pending 转停用 | 账号为 `revoke_pending` 且有旧错误 | POST owner `/logout` | 直接转 disabled，清空旧错误；不远端撤销、不删除 Token；后续 authorize 不被 pending 阻止 | P0 | V3 Sidecar 28/28 通过 |
| TC-037 | Disabled 禁止校验 | 账号为 disabled | POST owner/admin verify | 409 `x_account_disabled`；不读 Token、不 refresh、不调用 `/users/me`，状态保持 disabled | P0 | V3 Sidecar 28/28 通过 |
| TC-038 | 重新授权恢复 | 同 owner 的账号已 disabled | 重新走 authorize/callback | 更新原 row，覆盖保留旧 Token，清空停用字段，恢复 active 与 `publish_eligible=true` | P0 | V3 Sidecar 28/28 通过 |
| TC-039 | 发布凭证严格 active guard | active、disabled、error、缺 Access Token 各一组 | 进入 `publish_credentials` | 仅 active+Access Token 成功；敏感凭证只 yield `access_token`，不暴露完整 Token/Refresh Token；其他状态按白名单错误拒绝 | P0 | V3 Sidecar 28/28 通过；实际发布尚未接入 |
| TC-040 | 发布与停用并发 | 已进入 active `publish_credentials` 上下文 | 并发 POST `/logout` | 停用等待发布上下文退出；随后写 disabled；之后新发布上下文被拒绝，Token 仍保留 | P0 | V3 Sidecar 28/28 通过 |
| TC-041 | Legacy disconnected 与启动清理 | disabled Token 存在；disconnected 行有 live Token/`.*.disconnecting` | 启动/调用 cleanup | disabled Token 原字节保留；只清理 disconnected 残留并保持 legacy 状态兼容 | P0 | V3 Sidecar 28/28；legacy disconnected 重复 logout 清理自动化通过 |
| TC-042 | Backfill 默认 dry-run | owner 为空且可唯一匹配 | 不带参数运行脚本 | 输出 JSON `mode=dry-run`；不修改 DB；显示 legacy/resolvable/updated/unresolved 计数 | P0 | backfill 4/4 通过 |
| TC-043 | Legacy 唯一 owner apply | 旧 row 在 `drama_admin_user` 唯一匹配且 tenant 非空 | `--apply --require-all-resolved` | owner tenant/name/email/user 正确；row ID/Token hash/权限不变 | P0 | backfill 4/4；生产副本/live 各解析并更新 1 条，保全断言通过 |
| TC-044 | Legacy 零/多匹配门槛 | 主库无或多条候选 | dry-run `--require-all-resolved` | 不写 owner；JSON 仅含脱敏原因；退出码 2，阻断 apply/部署 | P0 | backfill 4/4；生产严格门槛为 1 resolved/0 unresolved，零/多分支未出现在 live |
| TC-045 | Backfill 幂等/并发保护 | 已回填或 apply 中数据被改动 | 重跑 apply/模拟 guarded update 数不符 | 已回填不重复写；并发不一致事务回滚且非零退出 | P0 | backfill 4/4 通过 |
| TC-046 | Admin no-store 与时间展示 | admin Cookie + 有刷新/同步/停用时间的数据 | 请求 API/Nginx 并打开页面 | admin API/Nginx `Cache-Control: no-store`，fetch `cache:no-store`；展示到期/刷新、校验/资料同步、更新/停用时间和 disabled 状态 | P1 | App/静态/Playwright 及生产未登录 owner/admin no-store 通过；V3 真实 Cookie 页面待验收 |
| TC-047 | 回滚含已 disabled 账号 | V3 已本地软停用且 Token 保留 | 执行回滚演练 | 不能回滚到会把 disabled Token 当 active 的旧代码；若无法保持 V3 状态投影与发布 guard，必须停止发布并隔离凭证 | P0 | 待执行 |
| TC-048 | 停用前签发的延迟 callback 最后写入胜出 | 同 owner 已取得 OAuth state，随后账号被软停用 | 停用完成后再用该 state 完成 callback | 视为用户显式重新授权；callback 的新 Token/资料最后写入，原 row 恢复 active、清空停用字段，旧保留 Token 被覆盖 | P0 | V3 Sidecar 28/28 通过 |

## 回归范围

- Feishu 登录、租户/用户会话解析、用户权限管理与 admin 判定。
- QuickNav 默认配置、数据库导航配置与公网静态副本。
- 主后台 health/auth API、原 X callback/health、internal 公网 404。
- 现有 drama/ad-control/playable API 冒烟；不得覆盖复合 `app.py` 的无关逻辑。
- V3 Sidecar 28/28、App contract 5/5、回填脚本 4/4 已通过；Python/JS/JSON、静态检查与 Playwright 需按 V3 页面复跑。生产回填/Token 保全、服务/API smoke、模块级查询与真实资料同步已有 V2 证据；真实 Cookie、软停用/恢复、发布 guard 与 rollback 另行验收。

## 生产验证门槛（V3）

- 部署前确认生产恰有预期 legacy 记录/Token；只记录数量、文件权限与 hash，不输出 Token。
- 先运行 backfill 默认 dry-run 与 `--require-all-resolved` 门槛，再执行 apply；迁移后 owner 唯一回填，原 row ID、x_user_id、Token hash/权限保持不变。
- 两个静态页面壳均可 200；非 admin 的 admin 页只显示权限门、不请求全量数据，admin API 必须 403；A/B/C Cookie 做真实隔离验证。
- 真实 X `/users/me` 同步后 admin 列表展示粉丝量等资料及刷新/同步/更新/停用时间；列表刷新本身不得触发 X API，admin 响应和页面请求必须 no-store。
- 经用户确认后对一条真实账号调用保留的 `/logout` 路由：验证状态变为 `disabled`、Token hash/权限及 `access_expires_at` 不变、日志中没有 X revoke/Token 读取或删除证据，重复调用幂等。
- 对生产 legacy `revoke_pending` 记录执行一次软停用迁移并验证旧错误清空；legacy `disconnected` 仍可展示且启动清理不触碰 disabled Token。
- 验证 disabled 账号的 verify 和发布 guard 均在读取 Token 前拒绝；随后由原 owner 重新授权，确认同一 row 恢复 active、Token 被新授权结果覆盖。
- 用受控发布上下文验证发布期间并发停用会等待，停用完成后新发布被拒绝；`publish_credentials` 只 yield `access_token`，实际 X Post HTTP 路径尚未接入，未来上游调用必须留在同一 context，不得表述为已完成端到端发帖。
- callback/软停用探针在 Nginx、sidecar、主 API 和审计日志中的敏感值命中数为 0。

V2 生产迁移与模块级 owner 隔离已有现场证据，但仍不能代替 V3 真实 Cookie、真实跨 owner OAuth、本地软停用/恢复或发布 guard 验收。V1/V2 历史证据继续保留，仅作迁移与回滚基线；远端 revoke 不再属于 V3 验收。
