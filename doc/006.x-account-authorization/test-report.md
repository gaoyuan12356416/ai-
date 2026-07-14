# 测试报告

## V3 本地软停用增量结论（2026-07-14）

> 本节是当前有效测试结论。下文 V2 的远端 revoke/`revoke_pending` 验收仅为历史证据，已被用户明确选择的 V3 本地软停用方案取代。

V3 本地回归通过，当前结论为“允许进入受控生产部署”，尚不是“V3 全量生产验收通过”。

- `POST /api/x-accounts/{id}/logout` 保持兼容路径，但只在本地写 `disabled`；自动化确认不读取 Token、不调用 X、不删除 Token，重复操作幂等。
- `disabled` 在列表中保持终态并输出 `publish_eligible=false`；owner/admin 校验 fail closed，同 owner 重新授权可恢复 `active`。
- legacy `revoke_pending` 即使 Token 不可读也可直接收敛为 `disabled`；legacy `disconnected` 继续保持历史终态，启动清理不会碰 `disabled` Token。
- `publish_credentials` 只接受精确 `active` 和非空 Access Token，敏感凭证只 yield `access_token` 字符串（不返回完整 Token 字典或 Refresh Token），并从锁内重查直到上游发布工作结束持续持有账号锁。实际 X Post 发布尚未接入；未来发布调用必须留在同一 context 内。
- 个人页和管理员页已统一“已停用/完成停用/更新与停用时间”语义，确认框明确 Token 保留且不会调用 X 解除授权。

V3 当前证据：

```text
python scripts/test_x_accounts.py -> Ran 28 tests, OK
python scripts/test_x_accounts_app_contract.py -> Ran 5 tests, OK
python -m py_compile app.py features/x_accounts/oauth_service.py features/x_accounts/client.py scripts/test_x_accounts.py scripts/test_x_accounts_app_contract.py -> exit 0
inline JavaScript syntax check -> x-accounts.html OK; x-account-list.html OK
Playwright mock owner flow -> pending “完成停用”确认框 -> POST 200 -> 已停用；console 0 error
Playwright mock admin flow -> 已停用/解除授权统计与筛选显示正确；console 0 error
git diff --check -> exit 0
```

生产待验证项：精确 commit/release/backup、目标 pending→disabled、目标及其他账号 Token hash/权限保全、两服务和页面/API smoke、日志检查，以及真实登录态个人页/管理员页显示。真实 X revoke 已退出当前范围，不应再执行。

## 测试结论

V2 已部署；本地 Sidecar 28/28、主后台 App 5/5、legacy owner backfill 4/4、全量 py_compile、两页 inline JS、QuickNav、navigation JSON、diff check 和 Playwright 三路径均通过，console 0 error。生产数据副本演练、live additive migration/backfill、Token 保全、服务/API smoke、模块级 owner/admin 查询及真实 `/2/users/me` 同步也已通过。

当前结论是“V2 已上线，已执行的有限生产验证通过”，不是“全量生产验收通过”。真实 Feishu Cookie 浏览器链路、真实跨 owner OAuth callback 和真实 X logout/revoke 尚未执行。

## 测试范围

- OAuth state/PKCE、Token 隔离/刷新、scope、Token identity、并发、internal bearer、日志脱敏和 30x Header 防泄漏。
- 个人/admin 页面与 API 拆分，`tenant_key + user_id` owner 边界，跨 owner verify/logout IDOR。
- 跨 owner callback 防覆盖与同 owner 重授权/断开后恢复。
- `/2/users/me` profile/public metrics 可空快照、admin 同步和安全 X 主页链接。
- Access-first/Refresh-last 双 revoke、confidential-client 请求、`revoke_pending`、失败保留 live Token、verify 禁止、logout 重试、本地删除失败与 `disconnected`。
- Owner 任一账号 pending 时禁止新 authorize；stale OAuth state callback 在 token request 前拒绝，owner lock 覆盖 precheck/exchange，logout 使用相同 owner→account 锁序。
- Sidecar 启动清理 `disconnected` 行对应的 live Token 与历史 `.*.disconnecting` tombstone。
- V1 旧 schema additive/idempotent migration；backfill 默认 dry-run、唯一匹配、`--require-all-resolved`、guarded apply 和零/多匹配 fail closed。
- 主 API actor、owner/admin scope、verify/logout、pending authorize gate 和 API Token admin gate 契约。
- Admin 主 API/Nginx/page fetch no-store、两页刷新/同步/更新/退出时间，以及 profile link `[A-Za-z0-9_]{1,50}` 边界。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞/待执行 |
| --- | --- | --- | --- | --- |
| V1 X 功能自动化（历史） | 16 | 16 | 0 | 0 |
| V2 Sidecar/Client 自动化 | 28 | 28 | 0 | 0 |
| V2 App contract | 5 | 5 | 0 | 0 |
| V2 Backfill 专项自动化 | 4 | 4 | 0 | 0 |
| V2 Python/JS/JSON/diff 检查组 | 1 组 | 1 组 | 0 | 0 |
| V2 Playwright 三路径 | 3 | 3 | 0 | 0 |
| 生产副本演练 + live legacy owner/Token 保全迁移 | 2 | 2 | 0 | 0 |
| 生产服务/API/模块查询 smoke | 1 组 | 1 组 | 0 | 0 |
| 真实 X `/2/users/me` 资料同步 | 1 | 1 | 0 | 0 |
| 登录态浏览器 Owner/Admin 验收 | 1 | 0 | 0 | 1 |
| 真实跨 owner OAuth callback | 1 | 0 | 0 | 1 |
| 真实 X logout/revoke | 1 | 0 | 0 | 1 |

## 缺陷情况

- BUG-001：callback 日志与精确代理，V1 已修复。
- BUG-002：Token 刷新/重新授权并发一致性，V1 已修复。
- BUG-003：Token 属主与必需 scope 校验，V1 已修复。
- BUG-004：跨用户全局列表/verify/logout IDOR，V2 已修复，最新核心自动化回归通过；生产 Cookie 验收待执行。
- BUG-005：跨 owner 重授权覆盖原 Token/归属风险，V2 已修复，最新核心自动化包含 Token 逐字节不变与最终 owner lock 回归；生产真实 OAuth 验收待执行。

历史独立安全复核曾提出“admin 可同步非本人账号”为 P1，但 `/api/admin/x-accounts/{id}/verify` 是本需求明确指定的 admin 同步能力，且只能经 Cookie admin 路由进入、不改变 owner，因此不作为适用缺陷。Owner 路由仍对非本人记录返回 404；pending、锁序与 backfill 最新差异已完成独立复核，未发现剩余 P0–P3。

## 验证证据

```text
python scripts/test_x_accounts.py -> Ran 28 tests, OK
python scripts/test_x_accounts_app_contract.py -> Ran 5 tests, OK
backfill专项自动化 -> Ran 4 tests, OK
python -m py_compile ... -> exit 0
node --check static/quick-nav.js -> exit 0
node --check <x-accounts inline script> -> exit 0
node --check <x-account-list inline script> -> exit 0
ConvertFrom-Json static/navigation.json -> success
git diff --check -> exit 0
Playwright admin-owner/admin-all/non-admin-gate -> passed, console 0 error
```

专项证据：

- Owner list 只返回联合 owner；非 owner verify/logout 404，且 mock 断言不读取 Token、不调用 revoke。
- Admin all query 与 admin verify 独立 scope 通过；普通用户 all query 返回 `x_admin_required`。
- 跨 owner callback 返回 `x_account_owned_by_other`；原 Token 文件逐字节一致，原 username/owner/authorized_by 不变。
- Logout 在远端调用前进入 `revoke_pending`，按 Access 后 Refresh 的固定顺序调用；任一步失败保留 live Token/pending，verify 返回 `x_disconnect_pending`，重试 logout 可完成，成功后才清凭证并标 `disconnected`。
- Owner 任一账号 pending 时 authorize 被拒；pending 产生前的 stale state callback 在 token request 前被拒。Authorize/callback/logout 使用统一 owner→account 锁序，不铸造远端孤儿 Token。
- 已 disconnected 的幂等 logout 与 sidecar 启动都会删除残留 live Token/`.*.disconnecting` tombstone。
- Backfill 默认 dry-run；仅唯一且 tenant 非空的匹配可 apply；零/多匹配在 `--require-all-resolved` 下退出 2；guarded update 不一致整笔回滚。
- Profile metrics 完整/缺失、verify 更新、username 1–50 位合法与超长/非法 profile URL 边界已纳入最新套件。

V1 生产历史：

- 提交：`eccabcb0d49714efa90403b140c0d2f77e5182dc`。
- 发布目录：`/root/releases/ai-x-account-authorization-eccabcb0d497`。
- 备份：`/root/backups/drama_material_service/20260714T041337Z-x-accounts-eccabcb`。
- 用户提供的生产页面截图显示已有 1 条授权账号；该截图是 legacy migration 的需求证据，但不是 V2 live DB/Token hash 证明。

V2 生产证据：

- 精确提交 `e00bd30adb466f92b38f218bfb7f288ea7ff0a69`；release `/root/releases/ai-x-account-authorization-e00bd30adb466`；部署前备份 `/root/backups/drama_material_service/20260714T070906Z-x-accounts-v2-e00bd30`。
- 备份保留部署前 live 基线清单 `source-live-manifest.sha256`，并以相对路径 `manifest.sha256` 完成目录内自校验；`sha256sum -c` 全部通过，备份目录 `0700`、清单/SQLite/Token `0600`。
- 迁移副本：`ensure_storage()` 成功；backfill dry-run 为 1 条可解析/0 条未解析，apply 更新 1 条。Live 使用相同门槛，dry-run 为 1 条可解析/0 条未解析，apply 更新 1 条。
- Live 原记录的 row ID、X user ID 与 `active` 状态保持，owner 已回填到唯一匹配的非空 tenant/user；真实账号标识不写入 Git。
- Token SHA-256 `cc6040d3f8e20a00561785f18209858ee3f89a5dd058a707cc66d9dea5888a6f`、权限 `0600`；在迁移、服务启动和真实 `/2/users/me` 同步后仍不变。Sidecar DB 权限 `0600`。
- 两个应用服务均 active。Nginx 非 systemd unit，`nginx -s reload` 成功；health/两个页面均 200，owner/admin API 未登录均 401 且 no-store。
- 模块级查询：回填 owner `mine=1`、admin `all=1`、其他 tenant/user `mine=0`；其他 owner 对现有记录执行 verify/logout 均返回 404，Token hash 不变。Live/release、公网静态副本与 Nginx 配置文件 hash 一致。
- 2026-07-14T07:17:10Z 真实 `/2/users/me` 同步成功，账号保持 active；followers/following/tweet/listed/like/media=`0/1/2/0/0/0`，Token hash 不变。
- 部署窗口应用 journal 的敏感字段命中为 0；Nginx 最近 500 条中固定字符串 `/x-oauth/callback?` 命中为 0。因本次未执行真实 callback/revoke，这不是完整 OAuth 流量泄漏验收。

## 遗留风险

- V2 已部署且唯一 legacy owner 已成功回填；零/多匹配的 fail-closed 行为由自动化覆盖，本次 live 数据没有该分支。
- X API 可用性/计费由平台控制；资料是 callback/主动同步时的快照，粉丝量不是实时值。
- OAuth revoke 是外部不可逆操作；本次为保护现有账号未执行真实 logout/revoke。Mock 不可替代真实撤销，回滚也不能恢复 X 侧已撤销的 Token。
- 未执行真实跨 owner OAuth callback；跨 owner 防覆盖结论当前来自自动化，不是生产真实 OAuth 证据。
- 未使用真实 Feishu Cookie 完成生产浏览器 owner/admin/非 admin 三路径；本地 Playwright 通过不能替代该项。
- 未执行 rollback 实际恢复演练；已验证部署前备份及非敏感 hash，但远端 revoke 后的回滚分支仍需单独受控验收。

## 发布建议

V2 可维持当前上线状态；迁移与现有账号保全无需重复执行。后续全量验收应单独安排：

1. 使用真实 admin/owner/非 admin Feishu Cookie 跑生产浏览器三路径，验证导航、全量/个人列表、权限门和 no-store。
2. 使用隔离测试 owner 执行真实跨 owner OAuth callback，确认冲突且原 owner/Token/时间不变；不得拿当前唯一生产账号做破坏性转移实验。
3. 仅在用户明确确认后执行真实 logout/revoke，观察 `revoke_pending`、Access-first/Refresh-last、失败重试、disconnected 清理与重新授权恢复。
4. 完成上述项目及受控 rollback 演练后，才能把结论升级为“全量生产验收通过”。
