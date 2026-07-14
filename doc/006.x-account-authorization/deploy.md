# 部署文档

## 变更内容

新增 X OAuth sidecar 多账号管理、AI后台代理 API、`x_accounts` 权限、页面和导航。

## 配置项

主后台 `.env`：

```text
X_POST_AUTOMATION_INTERNAL_URL=http://127.0.0.1:8810
X_POST_AUTOMATION_INTERNAL_TOKEN=<server-only>
X_POST_AUTOMATION_INTERNAL_TIMEOUT=30
```

sidecar `/etc/x-post-automation.env`：保留现有 Client ID/Secret/Callback/scopes，新增同值 `X_INTERNAL_TOKEN`、`X_ADMIN_RETURN_URL` 及 DB/Token目录。所有真实值仅在服务器。

## 数据库变更

sidecar 启动时幂等创建 `/var/lib/x-post-automation/accounts.sqlite3` 的账号/state表；不修改主业务 SQLite结构。

## 部署步骤

1. 推送 GitHub精确提交。
2. 记录生产文件 hash并备份 app、静态文件、systemd、Nginx和两个 env。
3. 服务器拉取/检出精确提交到发布目录。
4. 部署 feature文件、app.py、页面/导航、sidecar unit。
5. 生成并写入同一个 internal token到两个 root-only env。
6. Nginx callback精确 location设置 `access_log off`，列表精确/子路径设置 `no-store`，其余 `/x-oauth/*` 返回 404。
7. 校验 Python/JS/Nginx/systemd；先重启 sidecar，再重启主 API，最后 reload Nginx并同步静态文件。

## 验证步骤

- systemd status/journal、local/public health。
- 公网 callback/health可达，`/x-oauth/internal/accounts` 返回404。
- 未登录 API 401，登录浏览器页面权限与导航正确。
- 完成真实 OAuth后列表显示账号和五项scope。
- 搜索响应、DOM、日志、审计无敏感值。

## 生产部署记录（2026-07-14）

- GitHub 精确提交：`eccabcb0d49714efa90403b140c0d2f77e5182dc`。
- 服务器发布目录：`/root/releases/ai-x-account-authorization-eccabcb0d497`，已核对为上述精确提交。
- 部署前备份：`/root/backups/drama_material_service/20260714T041337Z-x-accounts-eccabcb`。
- 已部署主后台、X OAuth sidecar、页面/导航、systemd unit 和 Nginx 配置；服务器内部 token 已同步写入两个 root-only env，未输出真实值。
- `x-post-automation.service` 与 `drama-material-api.service` 重启后均为 active/running；Nginx 配置检查通过并完成 reload。
- 服务器 Python 3.9 环境执行 X 功能测试 16/16 通过。

## 生产验证结果（2026-07-14）

- sidecar 内部配置接口显示 OAuth 已配置，callback 为 `https://ai.yingliangads.com/x-oauth/callback`，五项必需 scope 完整。
- 公网 `/x-oauth/health` 与 `/x-accounts.html` 返回 200；未登录 `/api/x-accounts` 返回 401。
- 公网 `/x-oauth/internal/accounts` 返回 404，internal API 未暴露。
- 使用现有 API Token 访问 X 账号接口返回 403 `cookie_auth_required`，授权操作只接受后台 Cookie 会话。
- 伪 callback 请求返回 302；查询参数探针在 Nginx 日志和 sidecar journal 中命中数为 0，sidecar 仅记录请求方法与 `/callback` 路径。
- 权限模式已核对：`/var/lib/x-post-automation` 为 0700、`accounts.sqlite3` 为 0600、Token 目录为 0700、两个 env 为 0600。
- 真实 X 账号授权尚未执行，当前仅能确认服务、路由、配置、权限和日志保护；账号写入、真实 Token 刷新及授权后列表展示须由用户在 X 官方授权页确认后验收。

## 回滚方案

- 从 `/root/backups/drama_material_service/20260714T041337Z-x-accounts-eccabcb` 恢复部署前 app.py、静态文件、systemd unit、Nginx、env 及 X 数据备份。
- 恢复上一个 GitHub commit并重启受影响的两个服务。
- 保留 `/var/lib/x-post-automation` 数据，不删除授权记录，便于恢复和诊断。

## 注意事项

- 不整体覆盖生产复合单体前，必须以 live app.py做三方比对。
- Nginx收紧前确认 sidecar internal API只监听 loopback且有 token鉴权。
