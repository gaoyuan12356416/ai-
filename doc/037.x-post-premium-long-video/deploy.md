# 部署文档

## 变更内容

待提交后填写精确 commit。CPU X sidecar/main API/静态页与 GPU 媒体修复器需部署同一 GitHub 提交。

## 配置项

- `X_POST_DAILY_REPAIR_PROFILE` / schedule fallback：升级为 `x-h264-nvenc-720-duration-policy-v3`。
- 其余 X/COS/token 配置不输出、不改名。

## 数据库变更

- `x_authorized_account.subscription_type TEXT NOT NULL DEFAULT 'unknown'`
- `x_post_queue.preflight_duration REAL NOT NULL DEFAULT 0`

先对在线 SQLite backup 副本执行迁移演练，再由新 sidecar 幂等迁移 live DB。备份 token 目录并记录非敏感 SHA-256/权限，不输出内容。

## 部署步骤

1. 本地聚焦/回归通过，提交并推送 GitHub。
2. 记录 CPU/GPU 当前 release、unit 状态、相关文件 hash；创建双端时间戳备份和 SQLite online backup。
3. 在备份 DB 上运行两次迁移并核对 schema/count/quick_check/token manifest。
4. GPU 从精确 commit 建 immutable release，切换 `x-post-media-repair.service` 后验证健康。
5. CPU 从同一 commit 建 immutable X release；同步 main API 双运行时文件和静态页；安全更新 repair profile 配置。
6. 仅重启 `x-post-automation.service`、`drama-material-api.service` 和受影响的 GPU repair/tunnel unit；不手动启动调度 service。
7. 同步正式配置的 5 个账号会员快照，验证安全 DTO、DB 枚举和 token hash/mode。

## 验证步骤

- CPU/GPU py_compile、聚焦测试、SQLite `quick_check`。
- CPU `127.0.0.1:8810/health`、公共 `/x-oauth/health`、主 API/静态页 HTTP。
- GPU repair health 与 CPU tunnel health。
- 账号列表只出现安全会员字段；缺失/未知失败关闭。
- 调度 timer 仍启用，next trigger 正常；部署前后 queue/log/post 计数无测试性增加。

## 回滚方案

- 若发布安全不确定，先停止/禁用两个 schedule timer，再回滚代码。
- CPU/GPU current symlink 恢复各自 predeploy release，恢复 main API/静态文件和 repair profile 配置，窄重启服务。
- 保留 `/var/lib/x-post-automation` 全部数据库、token、队列和日志；不删除新列、不清除绑定。
- 不把迁移前 token 备份恢复成 active，仅用 hash/mode 做保全证据。

## 注意事项

- 不通过真实 X Post 验收。
- 当前自动发布上限为 600 秒/512 MiB，不等同于网页/iOS 会员产品上限。
- CPU/GPU profile 任一侧未同步时应失败关闭，不能临时放宽。
