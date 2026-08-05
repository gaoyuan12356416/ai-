# 部署文档

## 变更内容

- 生产只替换 `/etc/nginx/default.d/tt-drama-search.conf`。
- `/tt` 改为 alias 已上线的新 HTML；`/tt-code` 及其新 JS/API 配置不变。
- 不复制静态文件，不切换任何 current symlink，不重启应用服务。

## 部署前检查

1. 精确提交已推送 GitHub，服务器 release 文件与该提交 blob 哈希一致。
2. `tt-drama-featured.service` 当前不是 active/running；自然定时执行若正在进行，等待结束。
3. `drama-material-api`、`tt-post-service`、Redis 和 Nginx 健康。
4. 记录 `/opt/tt-post/current`、资源 current、旧配置与新旧静态文件哈希。
5. 在数据盘建立时间戳备份目录，保存旧 Nginx 配置、哈希清单和当前 symlink 记录。

## 部署步骤

1. 从 GitHub 精确提交建立只读 release。
2. 对 release 中的目标配置做哈希校验。
3. 复制目标配置到临时文件，再原子替换生产配置。
4. 执行 `nginx -t`；失败则立即恢复备份并停止。
5. reload Nginx，不 restart。
6. 轮询 `/tt`、`/tt-code`、新 JS 和两个新接口。

## 验证

- `/tt`、`/tt?source=bio`、`/tt-code` 均 200，无 Location，`Cache-Control: no-store`。
- `/tt` 与 `/tt-code` HTML SHA-256 相同，并引用新 JS。
- 旧 `tt-drama-search.html/js` SHA-256 与部署前一致。
- `/api/public/tt-code/resolve`、分语言榜单、旧 v1 endpoints 均健康。
- 真实浏览器执行语言、5 条榜单、拖动、Featured 拦截和剧 ID 搜索。
- `/opt/tt-post/current` 与资源 current 均保持部署前值。

## 回滚

1. 从本次数据盘备份恢复单个 `tt-drama-search.conf`。
2. 执行 `nginx -t`。
3. reload Nginx。
4. 验证 `/tt` 恢复旧 HTML，`/tt-code` 仍为新页，所有服务 current 未变化。

## 生产执行记录

待部署后填写精确提交、release、备份路径、配置哈希、验收时间和回滚命令。
