# 部署文档

## 变更内容

- 部署独立 GitHub commit 的 X sidecar release。
- 增量初始化发布队列/日志表。
- 在预期持久数据盘创建短链和临时媒体目录；Nginx 静态目录只增加受控 `s2l` 映射。
- 只重启 `x-post-automation.service`，不改/不重启主 AI 后台服务。

## 配置项

- `X_POST_SHORT_BASE_URL=https://ai.yingliangads.com/s2l`
- `X_POST_PUBLIC_ROOT=/mnt/data-disk/x-post-automation/s2l`
- `X_POST_MEDIA_ALLOWED_HOSTS=advertising-1306474899.cos.ap-hongkong.myqcloud.com`
- `X_POST_FFPROBE_BIN=/root/ffmpeg-static/ffprobe`
- `X_POST_MAX_MEDIA_BYTES`：默认 512 MiB，按 X 官方限制和生产素材上限取更小值。

现有 `X_CLIENT_ID`、`X_CLIENT_SECRET`、`X_INTERNAL_TOKEN`、Token 路径保持不变且不输出。

## 数据库变更

在 `/var/lib/x-post-automation/accounts.sqlite3` 中仅 `CREATE TABLE/INDEX IF NOT EXISTS` 新增 `x_post_queue` 和 `x_post_publish_log`。正式迁移前必须在生产副本演练，并核对旧表 count/schema、Token SHA-256 和 mode 不变。

## 部署步骤

1. 验证 `/mnt/data-disk` 挂载 UUID 与可用空间。
2. 用 SQLite backup API 备份账号库，复制 Token 目录并记录 SHA-256/mode；备份当前 unit 和 sidecar 源码。
3. 从 GitHub 拉取已推送的精确 commit 到 `/root/releases/ai-x-post-canary-<sha12>`，校验后原子切换 `/root/releases/ai-x-post-current` 符号链接。
4. 在备份副本运行迁移和自动化测试，确认旧数据不变。
5. 创建数据盘目录、权限和静态短链映射；部署跟踪的 unit，使其通过稳定链接指向精确 release，并增加所需 `ReadWritePaths`。
6. `systemctl daemon-reload`，只重启 `x-post-automation.service`。
7. 先完成 health/账号动态校验/短链 canary，再由 loopback internal API 发布一次。

## 验证步骤

- `systemctl is-active x-post-automation.service` 为 active，`Restart=always` 仍生效。
- loopback `/health` 与公网 `/x-oauth/health` 为 200；公网 `/internal/*` 仍不可访问。
- 账号发布前动态 `/users/me` 身份与目标 X user ID 一致。
- 短链公网 GET 可访问且目标参数精确；W2A 最终页面可达。
- X Create Post 返回 post ID，`x.com/<username>/status/<id>` 可访问。
- SQLite queue/log 均为 published；journal 和 DB 无敏感字段。

## 回滚方案

1. 停止 sidecar，把 `/root/releases/ai-x-post-current` 原子切回上一个精确 release；必要时恢复备份 unit。
2. 若新表未产生真实发布记录，可恢复部署前 SQLite 备份；若已成功发布，保留发布日志并只回滚代码，避免丢失审计事实。
3. 恢复短链映射前先保留已发布日志 ID 对应 HTML，防止已发 Post 中的链接失效。
4. Token 如发生正常刷新，不能用旧 Token 备份盲目覆盖；以当前轮换后的 Token 为准，仅在确认未轮换时才做整目录恢复。

## 注意事项

- 本次不安装 timer/cron。
- Create Post 结果不确定时不得重试，也不得换账号继续发同一素材。
- 最终部署记录需补充 commit、backup 路径、release 路径、重启时间、验证输出和日志 ID。
