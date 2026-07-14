# 测试报告

## 测试结论

V2 本地验证通过：Sidecar 28/28、主后台 App 5/5、legacy owner backfill 4/4、全量 py_compile、两页 inline JS、QuickNav、navigation JSON、diff check 和 Playwright 三路径均通过，console 0 error。

当前结论是“本地发布门槛通过”，不是“生产通过”。生产 owner 回填、Token hash 保全、真实 Cookie 生产验收、真实 `/2/users/me` 和真实 X revoke 尚未执行。

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
| 生产 legacy owner/Token 保全迁移 | 1 | 0 | 0 | 1 |
| 登录态浏览器 Owner/Admin 验收 | 1 | 0 | 0 | 1 |
| 真实 X 资料同步与 revoke | 1 | 0 | 0 | 1 |

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

## 遗留风险

- V2 尚未部署，线上仍是 V1 页面/接口语义；本地修复不等于线上已生效。
- 生产 legacy owner 必须从 `drama_admin_user` 唯一匹配回填；零/多匹配保持 admin-only，不能按 user_id 自动认领。
- 最新 backfill 自动化覆盖 dry-run/唯一匹配/fail closed/严格门槛，但生产主库回填、原 Token SHA-256/权限和 rollback 仍需现场验证。
- X API 可用性/计费由平台控制；资料是 callback/主动同步时的快照，粉丝量不是实时值。
- OAuth revoke 是外部不可逆操作；Mock 不可替代真实撤销，回滚也不能恢复 X 侧已撤销的 Token。
- 静态检查不能替代登录态浏览器对导航、权限门、表格和操作确认的验收。

## 发布建议

建议进入 GitHub-first 生产部署；随后必须按以下步骤执行并更新本文档：

1. 服务器检出精确 commit，部署前备份 app、静态、Nginx/systemd、两个 env、SQLite 与 Token 目录。
2. Live 只读盘点确认预期 legacy 记录；记录 row/token 非敏感 hash，执行 additive migration，再依次运行 backfill `--require-all-resolved` dry-run 与 `--apply --require-all-resolved`。
3. 迁移后断言 row ID/x_user_id/token_store_key、Token SHA-256/权限不变；不满足立即回滚。
4. A/B/C Cookie 验证 owner/跨 tenant 隔离；admin 验证全量列表与同步，非 admin 只能看到权限门/API 403。
5. 完成真实 `/2/users/me` 资料快照；经用户确认后执行一次真实 logout，观察 `revoke_pending`、Access-first/Refresh-last、失败重试与 disconnected 清理，再验证重新授权恢复。
6. Nginx/sidecar/main/audit 日志敏感信息扫描为 0，再给出生产通过结论。
