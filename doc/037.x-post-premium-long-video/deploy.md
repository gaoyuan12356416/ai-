# 部署文档

## 变更内容

CPU X sidecar/main API/静态页与 GPU 媒体修复器部署同一 GitHub 提交
`29bd90034396c597b30ceb7135376efb750ec886`。素材池允许加入最长 600 秒视频；
每次排期及最终发布前都通过个号 token 的 `/2/users/me` 返回值刷新
`subscription_type`，仅 `basic`、`premium`、`premium_plus` 账号可承接超过
140 秒的视频，缺失或未知值失败关闭。

## 2026-08-10 生产结果

- GitHub 分支：`codex/x-post-premium-long-video-20260810`；提交：
  `29bd90034396c597b30ceb7135376efb750ec886`。
- CPU current：
  `/mnt/data-disk/x-post-automation/releases/29bd90034396c597b30ceb7135376efb750ec886`；
  发布前 release：
  `/mnt/data-disk/x-post-automation/releases/0d36c7b56b8b415a1ab5776249540c5a7c0e8fb6`。
- GPU current：
  `/opt/x-post-media-repair/releases/29bd90034396c597b30ceb7135376efb750ec886`；
  发布前 release：
  `/opt/x-post-media-repair/releases/b6f95f3874a9bb187aa7e8c7faac6254893ba787`。
- CPU 回滚包：
  `/mnt/data-disk/x-post-automation/backups/20260810T183611-premium-long-video`；
  GPU 回滚包：
  `/data/x-post-media-repair/backups/20260810T183611-premium-long-video`。
- 迁移演练在 online backup 副本上连续执行两次，`quick_check=ok`；账号、队列、
  发布日志、素材池计数保持 `15/150/150/184`，新增两列均存在。
- 5 个正式排期账号均用各自 token 在 `2026-08-10T10:42:06Z` 同步成功：
  `16 @zinonymouss`、`13 @_shaniyalanae`、`14 @MKSawyer313`、
  `15 @WitTheGoodHair`、`5 @Kkkkkk2016911` 均为 `active`、
  `publish_approved=true`、`subscription_type=none`。因此发布时没有任何账号可承接
  超过 140 秒的视频；长视频会保留为未绑定素材，等待以后出现会员账号。
- GPU 本机及 CPU 反向隧道健康均返回
  `x-h264-nvenc-720-duration-policy-v3`；CPU sidecar/main API 均为 active，
  公共 health 与 3 个静态页返回 HTTP 200 且与 release hash 一致。
- 18:44 自然 claim 轮询返回 `claimed_or_pending_count=0`；18:44:10 自然 scheduler
  返回 `status=no_due`。发布后队列/发布日志仍为 `150/150`，没有为验收创建真实 X Post。
- `x-post-schedule.timer` 与 `x-post-schedule-claim.timer` 已恢复为
  `active/enabled`。

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
