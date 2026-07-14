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

## 回滚方案

- 恢复部署前 app.py、静态文件、systemd unit、Nginx和 env备份。
- 恢复上一个 GitHub commit并重启受影响的两个服务。
- 保留 `/var/lib/x-post-automation` 数据，不删除授权记录，便于恢复和诊断。

## 注意事项

- 不整体覆盖生产复合单体前，必须以 live app.py做三方比对。
- Nginx收紧前确认 sidecar internal API只监听 loopback且有 token鉴权。
