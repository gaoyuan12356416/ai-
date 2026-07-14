# 测试报告

## 测试结论

功能已按 GitHub 精确提交部署到生产，服务、API边界、Cookie鉴权、必需 scope 配置、日志脱敏和文件权限验证通过。真实 X OAuth 闭环尚未通过：必须由用户在 X 官方页面确认授权后，才能验收账号入库、授权列表、真实 Token 与刷新流程。

## 测试范围

- OAuth state/PKCE、过期/重放、多账号 upsert和 Token文件隔离。
- Scope下限、Token过期/刷新/撤销、Refresh Token轮换和 Token属主。
- 双 verify及 callback-vs-verify并发。
- loopback/internal bearer、30x Authorization防泄漏、callback日志脱敏。
- AI 后台权限路由、错误白名单、页面/导航/Nginx/systemd配置。
- 主单体及规定回归模块编译。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| X功能自动化 | 16 | 16 | 0 | 0 |
| Python编译组 | 11 | 11 | 0 | 0 |
| JS/JSON/diff静态检查 | 4 | 4 | 0 | 0 |
| 两轮只读代码评审 | 2 | 2 | 0 | 0 |
| 生产部署与基础设施验证 | 1 | 1 | 0 | 0 |
| 登录态浏览器与真实 X OAuth | 1 | 0 | 0 | 1（待用户授权） |

## 缺陷情况

- BUG-001：callback日志与精确代理，已修复。
- BUG-002：Token刷新/重新授权并发一致性，已修复。
- BUG-003：Token属主与必需 scope校验，已修复。
- 最终复核未发现仍然确定的 P0/P1。

## 验证证据

```text
python scripts/test_x_accounts.py -> Ran 16 tests, OK
python -m py_compile ... -> exit 0
node --check static/quick-nav.js -> exit 0
node --check <x-accounts inline script> -> exit 0
ConvertFrom-Json static/navigation.json -> success
git diff --check -> exit 0
server Python 3.9 scripts/test_x_accounts.py -> Ran 16 tests, OK
```

### 生产部署与验证证据（2026-07-14）

- 部署提交：`eccabcb0d49714efa90403b140c0d2f77e5182dc`。
- 发布目录：`/root/releases/ai-x-account-authorization-eccabcb0d497`。
- 部署前备份：`/root/backups/drama_material_service/20260714T041337Z-x-accounts-eccabcb`。
- `x-post-automation.service`、`drama-material-api.service` 均为 active/running；Nginx 配置检查通过并完成 reload。
- sidecar internal config 显示 configured=true、callback 正确、`tweet.read tweet.write users.read offline.access media.write` 五项必需 scope 完整。
- `/x-oauth/health` 与 `/x-accounts.html` 为 200；未登录 `/api/x-accounts` 为 401；公网 `/x-oauth/internal/accounts` 为 404；API Token 请求为 403 `cookie_auth_required`。
- callback 日志探针未在 Nginx 日志或 sidecar journal 中发现 query 标记，sidecar 只记录方法与路径。
- `/var/lib/x-post-automation`、`accounts.sqlite3`、Token 目录、两个 env 权限依次为 0700、0600、0700、0600。

## 遗留风险

- 真实 X OAuth必须由用户在 X官方页面确认；本地测试使用 Mock X响应，不能代替真实授权。
- X API可用性/计费由平台控制；页面不自动轮询，只在主动校验时请求。

## 发布建议

生产基础设施部署已完成，可进入用户验收。下一步由用户登录 AI 后台并在 X 官方授权页确认；回跳后核对账号列表、五项 scope、首次/最近授权时间、Token 到期时间、状态与主动校验。未完成该步骤前，不得标记真实 OAuth、真实账号持久化或真实 Token 刷新为通过。
