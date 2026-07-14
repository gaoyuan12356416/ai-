# 测试用例

## 测试范围

- V1 回归：Cookie/module 鉴权、OAuth state/PKCE、Token 隔离/刷新、并发、日志脱敏、Nginx/internal 边界。
- V2 新增：个人页与 admin 页拆分、`tenant_key + user_id` owner 隔离、IDOR、防跨 owner 重授权覆盖、资料快照、admin 同步、`revoke_pending` 可恢复退出状态机、启动残留清理、legacy 回填 CLI 与回滚。

## 测试数据

- Owner A：`tenant=t1,user=u1`；Owner B：`tenant=t1,user=u2`；Owner C：`tenant=t2,user=u1`，用于验证同 user ID 跨租户隔离。
- Admin：独立 admin Cookie；普通用户：有/无 `x_accounts` 权限各一组；API Token 一组。
- Mock X-A、X-B 两个不同 `x_user_id`，以及同一个 X-A 被不同 owner 重新授权的冲突数据。
- Mock `/2/users/me` 完整/缺失可选字段、public_metrics 变化、Token refresh、Access-first/Refresh-last revoke 成功/已撤销/单项失败/网络失败、本地删除失败与启动残留清理。
- V1 生产副本：1 条 `x_authorized_account`、对应 Token 文件和旧 schema，用非敏感 hash 验证迁移前后不变。
- Legacy 回填副本：唯一/零/多条 `drama_admin_user` 匹配，覆盖默认 dry-run、`--apply`、`--require-all-resolved` 与并发条件更新。
- 真实生产 X 账号只用于最终 callback、资料同步与 revoke 验收；测试日志不得输出 Token。

## 用例列表

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| TC-001 | 未登录访问个人列表 | 无 Cookie | GET `/api/x-accounts` | 401 | P0 | V1 生产历史通过；V2 生产待回归 |
| TC-002 | 无模块权限 | 普通用户 | GET/POST owner API | 403 | P0 | App 5/5 与代码复核通过；生产待回归 |
| TC-003 | API Token 尝试读取/授权 | 有 API Token | GET list、POST authorize | 403 `cookie_auth_required` | P0 | V1 生产历史通过；V2 App/Sidecar 本地通过，生产待回归 |
| TC-004 | 发起授权 | 有权限 Cookie | POST authorize | URL 含五项 scope、S256、正确 callback；state 保存 tenant/user | P0 | Sidecar 28/28 通过 |
| TC-005 | state 错误/过期/重放 | 构造异常 callback | 访问 callback | 拒绝且不写 Token | P0 | Sidecar 28/28 通过 |
| TC-006 | 首次授权与资料快照 | Mock token/user 成功 | callback | owner 正确、新增 1 行、Token 0600、完整资料快照 | P0 | Sidecar 28/28 通过 |
| TC-007 | 同 owner 重复授权 | 同一 owner + x_user_id | 再次 callback | 仍 1 行，owner/首次授权不变，Token/最近授权/快照更新 | P0 | Sidecar 28/28 通过 |
| TC-008 | 同 owner 多账号 | X-A、X-B | 两次 callback | 个人列表 2 行且均属当前 owner | P0 | Sidecar 28/28 通过 |
| TC-009 | scope 缺失 | Token 少一项 scope | callback | `scope_missing` 并列出缺失权限 | P0 | Sidecar 28/28 通过 |
| TC-010 | Access Token 到期 | 有 Refresh Token | 个人/admin 列表 | `refresh_required`，列表不请求 X | P1 | Sidecar 28/28 通过 |
| TC-011 | 本人主动校验/刷新 | Token 过期 | POST owner verify | 刷新/轮换 Token、状态 active、资料快照更新 | P0 | Sidecar 28/28 通过 |
| TC-012 | 上游已撤销 | refresh `invalid_grant` | POST verify | 状态 revoked，错误脱敏 | P0 | Sidecar 28/28 通过 |
| TC-013 | Internal 接口鉴权 | 无/错 internal token | 访问 `/internal/*` | 403 | P0 | Sidecar 28/28 通过 |
| TC-014 | 敏感数据泄漏扫描 | 完成授权/校验/logout | 搜索 API/HTML/log/audit | 无 Secret/Token/code/state/verifier/Basic header | P0 | 本地敏感信息扫描通过；生产日志待执行 |
| TC-015 | 页面与导航拆分 | 各角色 Cookie | 打开两个页面/导航 | 个人入口按模块权限；`xAccountList` 仅 admin；非 admin 打开静态 admin 页只见权限门且不请求数据 | P0 | JS/JSON 与 Playwright 三路径通过；生产 Cookie 待回归 |
| TC-016 | 服务重启恢复 | 多 owner + disconnected 数据 | 重启两个服务 | owner/快照/状态不丢；disconnected 凭证残留被清理 | P1 | Sidecar 28/28 通过；生产待回归 |
| TC-017 | Token 属主不一致 | `/users/me` 空或错误 ID | verify | `x_identity_mismatch`，不标 active | P0 | Sidecar 28/28 通过 |
| TC-018 | 必需 scope 配置被删减 | env 缺 `media.write` | 启动/发起授权 | fail closed | P0 | Sidecar 28/28 通过 |
| TC-019 | 并发 verify/callback/logout | 并发请求 | 执行操作 | 同 `x_user_id` 串行，无旧 Token 覆盖新 Token | P0 | Sidecar 28/28 并发回归通过 |
| TC-020 | 上游跨域 30x | 目标收集 Header | token/user/revoke 请求 | 拒绝跳转，不转发 Authorization | P1 | Sidecar 28/28 通过 |
| TC-021 | Owner A 个人列表隔离 | A/B/C 各有账号 | A GET `/api/x-accounts` | 只返回 `t1+u1`，不返回 B/C | P0 | BUG-004 修复；Sidecar 28/28 通过 |
| TC-022 | 同 user ID 跨 tenant 隔离 | A=`t1+u1`、C=`t2+u1` | 分别 GET 个人列表 | 两者列表互不重叠 | P0 | Sidecar 28/28 通过；生产 Cookie 待回归 |
| TC-023 | 跨 owner verify IDOR | A 知道 B 的记录 ID | A POST `/{id}/verify` | 404；不调用 X，不改变 B 记录/Token | P0 | BUG-004 修复；Sidecar 28/28 通过 |
| TC-024 | 跨 owner logout IDOR | A 知道 B 的记录 ID | A POST `/{id}/logout` | 404；不调用 revoke，不改变 B 数据 | P0 | BUG-004 修复；Sidecar 28/28 通过 |
| TC-025 | 非 admin 全量 API | 普通有模块权限用户 | GET `/api/admin/x-accounts`、POST admin verify | 403，响应不泄漏总数/记录 | P0 | App 5/5 通过；生产待回归 |
| TC-026 | Admin 全量列表 | admin Cookie | GET `/api/admin/x-accounts` | 返回所有 owner/账号快照，含 owner tenant/user 和状态 | P0 | Sidecar 28/28 与 Playwright 通过；生产待回归 |
| TC-027 | Admin 同步 | admin + 任意 owner 账号 | POST `/api/admin/x-accounts/{id}/verify` | 刷新状态/资料并审计，owner 不变 | P0 | Sidecar 28/28 通过；生产待回归 |
| TC-028 | 跨 owner 重授权 | X-A 已属 A，B callback X-A | 完成 token/user response | 409 `x_account_owned_by_other`；A 的 owner、Token hash、时间/快照不变 | P0 | BUG-005 修复；Sidecar 28/28 通过 |
| TC-029 | owner 空 legacy 不可认领 | legacy row owner tenant 空 | 同 user_id 普通用户 GET/verify | 列表不可见、操作 404；仅 admin 可见 | P0 | Sidecar 28/28 通过 |
| TC-030 | `/users/me` 字段快照 | 完整字段响应 | callback 后查 API/DB | 名称、头像、location、verified/protected、metrics、X 建档/同步时间正确 | P1 | Sidecar 28/28 通过 |
| TC-031 | 可选资料缺失 | location/metrics 缺失 | callback/verify | 空值安全，页面不崩溃，不用错误账号值覆盖有效快照 | P1 | Sidecar 28/28 与 Playwright 通过；生产待回归 |
| TC-032 | X 主页链接边界 | 16 字符、50 字符、51 字符及不安全字符 username | 打开 admin/owner 页面 | 1–50 位 `[A-Za-z0-9_]` 生成 `https://x.com/<username>`；超长/非法值不生成链接 | P1 | Sidecar 28/28、Playwright 通过 |
| TC-033 | 本人 logout 成功 | A active，access+refresh 存在 | POST owner logout | 远端调用前写 `revoke_pending`；Access 先、Refresh 最后；成功后才删凭证并写 `disconnected` 字段 | P0 | Sidecar 28/28 Mock 通过；真实 revoke 待执行 |
| TC-034 | Logout 已断开幂等清理 | 账号已 disconnected，残留 live Token/tombstone | 再次 POST logout | 不发起远端 revoke；删除残留凭证并保持 disconnected | P1 | Sidecar 28/28 通过 |
| TC-035 | Access revoke/网络失败 | 第一项 Access revoke 失败 | POST logout | 502 `x_disconnect_failed`；状态 `revoke_pending`、live Token 保留、错误脱敏 | P0 | Sidecar 28/28 通过 |
| TC-036 | Refresh 最后撤销失败 | Access 已成功，Refresh revoke 失败 | POST logout | 502 `x_disconnect_failed`；保持 `revoke_pending` 与 live Token，不误标 disconnected | P0 | Sidecar 28/28 通过 |
| TC-037 | Pending 禁止校验 | 账号为 `revoke_pending` | POST owner/admin verify | 409 `x_disconnect_pending`；不 refresh、不调用 `/users/me` | P0 | Sidecar 28/28 通过 |
| TC-038 | Pending 重试退出 | TC-035/036 后上游恢复 | 再次 POST owner logout | 允许重试；Access/Refresh 按序幂等处理；最终删凭证并标 `disconnected` | P0 | Sidecar 28/28 通过 |
| TC-039 | 本地凭证删除失败 | 两次远端 revoke 成功，模拟 unlink 失败 | POST logout | 502 `x_disconnect_failed`；状态仍为 `revoke_pending`，不写 disconnected | P0 | Sidecar 28/28 通过 |
| TC-040 | Revoke confidential contract | Mock 捕获请求 | logout | URL/POST/form token 正确；Basic 为 client id/secret；无 `token_type_hint` 依赖 | P0 | Sidecar 28/28 通过 |
| TC-041 | 启动清理旧凭证残留 | disconnected 行旁有 live Token 和多个 `.*.disconnecting` | 启动 sidecar | 加账号锁删除 live Token/tombstone；DB 保持 disconnected；其他账号不受影响 | P0 | Sidecar 28/28 通过 |
| TC-042 | Backfill 默认 dry-run | owner 为空且可唯一匹配 | 不带参数运行脚本 | 输出 JSON `mode=dry-run`；不修改 DB；显示 legacy/resolvable/updated/unresolved 计数 | P0 | backfill 4/4 通过 |
| TC-043 | Legacy 唯一 owner apply | 旧 row 在 `drama_admin_user` 唯一匹配且 tenant 非空 | `--apply --require-all-resolved` | owner tenant/name/email/user 正确；row ID/Token hash/权限不变 | P0 | backfill 4/4 通过；生产执行待完成 |
| TC-044 | Legacy 零/多匹配门槛 | 主库无或多条候选 | dry-run `--require-all-resolved` | 不写 owner；JSON 仅含脱敏原因；退出码 2，阻断 apply/部署 | P0 | backfill 4/4 通过；生产演练待完成 |
| TC-045 | Backfill 幂等/并发保护 | 已回填或 apply 中数据被改动 | 重跑 apply/模拟 guarded update 数不符 | 已回填不重复写；并发不一致事务回滚且非零退出 | P0 | backfill 4/4 通过 |
| TC-046 | Admin no-store 与时间展示 | admin Cookie + 有刷新/同步/退出时间的数据 | 请求 API/Nginx 并打开页面 | admin API/Nginx `Cache-Control: no-store`，fetch `cache:no-store`；展示到期/刷新、校验/资料同步、更新/退出时间 | P1 | App 5/5、静态与 Playwright 通过；生产 Nginx 待验收 |
| TC-047 | 回滚含已 disconnect 账号 | V2 已远端撤销 | 执行回滚演练 | 不把备份旧 Token 当 active；标需重授权/隔离旧凭证 | P0 | 待执行 |

## 回归范围

- Feishu 登录、租户/用户会话解析、用户权限管理与 admin 判定。
- QuickNav 默认配置、数据库导航配置与公网静态副本。
- 主后台 health/auth API、原 X callback/health、internal 公网 404。
- 现有 drama/ad-control/playable API 冒烟；不得覆盖复合 `app.py` 的无关逻辑。
- Sidecar 28/28、App contract 5/5、回填脚本 4/4、Python/JS/JSON、静态检查与 Playwright 三路径均已通过；生产回填/rollback 另行验收。

## 生产验证门槛（V2）

- 部署前确认生产恰有预期 legacy 记录/Token；只记录数量、文件权限与 hash，不输出 Token。
- 先运行 backfill 默认 dry-run 与 `--require-all-resolved` 门槛，再执行 apply；迁移后 owner 唯一回填，原 row ID、x_user_id、Token hash/权限保持不变。
- 两个静态页面壳均可 200；非 admin 的 admin 页只显示权限门、不请求全量数据，admin API 必须 403；A/B/C Cookie 做真实隔离验证。
- 真实 X `/users/me` 同步后 admin 列表展示粉丝量等资料及刷新/同步/更新/退出时间；列表刷新本身不得触发 X API，admin 响应和页面请求必须 no-store。
- 经用户确认后做一次真实 logout，验证 `revoke_pending`、Access-first/Refresh-last、失败重试、`disconnected`、本地 Token/旧 tombstone 清理和重新授权恢复。
- callback/revoke 探针在 Nginx、sidecar、主 API 和审计日志中的敏感值命中数为 0。

现有 V1 生产证据（提交 `eccabcb0d49714efa90403b140c0d2f77e5182dc`、服务 active、16/16、日志/文件权限）继续保留，但不能代替 V2 owner 隔离、legacy migration 或真实 revoke 验收。
