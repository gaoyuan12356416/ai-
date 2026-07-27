# 部署文档

## 变更内容

- 新增公开 TT drama resolver、只读查询和内存缓存。
- 更新 `/tt` 动态剧集卡片、封面加载和错误处理。
- 更新 Nginx CSP 允许同源 API与封面 CDN。

## 配置项

- `TT_DRAMA_RESOLVER_APP_ID=1479`
- `TT_DRAMA_RESOLVER_CACHE_TTL_SECONDS=3600`
- `TT_DRAMA_RESOLVER_NEGATIVE_TTL_SECONDS=300`
- `TT_DRAMA_RESOLVER_STALE_TTL_SECONDS=21600`
- `TT_DRAMA_RESOLVER_CACHE_MAX_ENTRIES=10000`
- `TT_DRAMA_RESOLVER_DB_MAX_CONCURRENCY=4`
- `TT_DRAMA_RESOLVER_DB_CONNECT_TIMEOUT_SECONDS=2`
- `TT_DRAMA_RESOLVER_DB_READ_TIMEOUT_SECONDS=3`
- `TT_DRAMA_RESOLVER_RATE_LIMIT_PER_MINUTE=30`
- `TT_DRAMA_RESOLVER_MAX_INFLIGHT=32`

## 数据库变更

无 DDL、无写入。只使用现有 `DRAMA_DB_*` / `ADMIN_MAPPING_MYSQL_*` 配置连接 63350 只读端点。

## 部署步骤

1. 本地测试通过后提交并推送 GitHub，记录代码 commit。
2. 记录线上服务状态、源文件哈希和 API 健康状态。
3. 创建 `/root/backups/drama_material_service/<timestamp>-tt-drama-resolver`，备份将变更的后端、静态和 Nginx 文件。
4. 从 GitHub 精确 commit 建立 `/root/releases/ai-tt-drama-resolver-<sha>`。
5. 先安装后端模块、`app.py` 和配置示例；执行 `py_compile`。
6. 安装两份 TT 静态文件和 Nginx exact location 配置；执行 `nginx -t`。
7. 重启 `drama-material-api.service`，仅 reload Nginx。
8. 验证 loopback/public API、`/tt`、冷/热查询、移动端和真实 W2A 点击。

Nginx 必须同时发布精确的
`location = /api/public/tt-drama/resolve`，代理到 `127.0.0.1:8787` 并设置
`X-Real-IP` / `X-Forwarded-For`；仅更新 `/tt` 静态页不能完成本需求。

实际 commit、release、backup 和命令在发布后补录。

## 验证步骤

- `systemctl is-active drama-material-api.service nginx`
- `curl` 验证 200/400/404/503/429、no-store、Server-Timing 与缓存状态。
- 冷查一次、热查至少 5 次并记录 TTFB/total。
- 单独测量返回的封面 URL。
- 390x844 浏览器验证命中、未命中、封面失败、参数透传和 CTA。
- 比较 GitHub release、运行目录和 Nginx 发布目录 SHA-256。

## 回滚方案

1. 从备份恢复 `app.py`、新增 feature 目录、两份静态文件和 Nginx 配置。
2. 执行 `python3 -m py_compile /root/drama_material_service/app.py` 与 `nginx -t`。
3. 重启 `drama-material-api.service`，reload Nginx。
4. 验证 `/api/auth/status` 和原 `/tt` 页面。

具体备份路径和可复制命令在部署后补录。

## 注意事项

- 运行目录不是 Git worktree，不能把文件复制称为 GitHub 部署；release 必须从服务器 GitHub fetch/checkout 的精确 commit 生成。
- 只同步本需求明确文件，不覆盖整个线上 static 目录。
- 不输出 `.env`、数据库密码或内部连接信息。
