# 部署文档

## 变更内容

- 只更新独立 `/tt-code` 的 HTML/JS，旧 `/tt` 静态文件不变。
- 增加 `/api/public/tt-drama/featured-by-language` 静态 JSON 端点。
- Featured 定时任务同时生成旧 v1 文件和新 schema v2 分语言文件。
- 不更新 `app.py`，不切换 `/opt/tt-post/current`，避免覆盖并行 TT 发布功能。

## 配置项

- v1 输出：`/mnt/data-disk/tt-drama-featured/public/current.json`。
- v2 输出：`/mnt/data-disk/tt-drama-featured/public/current-by-language.json`。
- 代码 current：`/mnt/data-disk/tt-drama-resource-cache/current`。
- systemd 单次刷新最长 15 分钟；原有 Asia/Shanghai 定时计划不变。

## 数据库变更

无建表、无 DDL、无写数据库操作。刷新任务只读查询 `ads_custom_source_insight.drama_language` 与消费数据。

## 部署步骤

1. 将本分支提交并推送 GitHub，记录精确提交 SHA。
2. 重新读取生产 symlink、静态文件哈希、systemd/Nginx 配置和任务状态。
3. 在数据盘创建带时间戳备份，保存旧静态文件、Nginx、systemd unit 与 symlink 目标。
4. 由 GitHub 精确提交创建 `/mnt/data-disk/tt-drama-resource-cache/releases/<sha>`。
5. 只切换资源缓存 current；复制 `/tt-code` 静态文件、Nginx 配置和 Featured unit。
6. 执行 `systemctl daemon-reload`、`nginx -t`，校验成功后 reload Nginx。
7. 手工启动一次 `tt-drama-featured.service`，监控耗时、日志及产物，再保留原定时器。

## 验证步骤

- v2 端点返回 200、schema_version=2、default_language=en，每桶恰好 5 条、无消费字段。
- 浏览器 `en-US`、`zh-CN`、`zh-TW`、`ar` 和未知语言分别展示正确 UI/榜单；标题精确且无副标题。
- 拦截 Featured 点击跳转，确认 `af_channel=Featured`；拖动不跳转。
- 旧 `/tt`、旧 v1 Featured 端点和 `/opt/tt-post/current` 保持不变。
- 检查 public 静态文件哈希等于已发布 GitHub 提交。

## 回滚方案

1. 从本次备份恢复 `/tt-code` HTML/JS、Nginx 配置和 systemd unit。
2. 将 `/mnt/data-disk/tt-drama-resource-cache/current` 原子切回备份记录的旧 release。
3. 执行 `systemctl daemon-reload`、`nginx -t`，成功后 reload Nginx。
4. 重新启动一次 Featured 服务并验证旧 v1 端点；新 v2 文件可保留为无引用产物。

生产备份路径、提交 SHA 和精确回滚命令在部署完成后补录。

## 注意事项

- 部署前如发现 Featured 刷新正在运行，等待完成后再切换 current。
- 首次生产刷新需实测总耗时低于 15 分钟，不能只依据本地热缓存结果。
- 任何一步校验失败时停止后续步骤；不触发发布广告或真实外链访问。
