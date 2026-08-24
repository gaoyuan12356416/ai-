# 部署文档

## 变更内容

仅切换 X immutable CPU release 中的 schedule runner；无 schema、静态页或主 API 变化。

## 配置项

复用生产已有 repair URL/profile/budget/timeout，不得输出 repair token。

## 数据库变更

无。部署前仍创建 SQLite online backup 并验证 `quick_check`。

## 部署步骤

1. 推送精确 GitHub commit。
2. 记录 current release、timer/unit 状态、队列/log/unknown 基线。
3. 暂停 schedule/claim timer 并等待 oneshot 退出。
4. 创建在线 SQLite 备份、unit/env/hash/timer manifest。
5. 从 GitHub 精确 commit 创建 immutable release。
6. 在 release 内执行 py_compile 和 focused tests。
7. 原子切换 `/opt/x-post-automation/current`。
8. 恢复原 timer 状态；不手工启动发布 oneshot。

## 验证步骤

- GPU repair health/profile。
- current symlink 和 GitHub commit 一致。
- schedule/claim timer active，下一触发正常。
- 自然 `no_due`/零 claim。
- queue/log/post/unknown 计数无部署性变化。
- SQLite `quick_check=ok`、foreign key=0。

## 回滚方案

停止 schedule/claim timer，切回部署前 release，恢复 timer 状态。保留当前 SQLite、Token 和历史 queue/log，不恢复旧数据库覆盖新事实。

## 注意事项

历史 run 318/320 及其 failed queue 不自动重试。

## 生产执行记录

- 运行代码提交：`61f2f420cca1a63d1c2aa83e9a70cc960790a64a`。
- 当前 release：`/mnt/data-disk/x-post-automation/releases/61f2f420cca1a63d1c2aa83e9a70cc960790a64a`。
- 部署前备份：`/mnt/data-disk/x-post-automation/backups/20260824T155659+0800-schedule-media-preflight-repair-pre-61f2f42`，目录权限 `0700`，代码和 SQLite checksum 通过。
- 回滚 release：`/mnt/data-disk/x-post-automation/releases/2f9d31d8ce782f59d38b3a64b3c3b35cd38f8089`。
- schedule/claim timer：均为 `enabled/active`；自然执行分别返回 `no_due` 与 `claimed_or_pending_count=0`。
- 台账：queue/log `606/606`，published `548`，failed `58`，unknown `0`，active run `0`；`quick_check=ok`，foreign key error `0`。
- GPU repair health：`status=ok`，profile `x-h264-nvenc-720-duration-policy-v5`。
- 未手工启动发布 oneshot，未创建部署测试 Post。
