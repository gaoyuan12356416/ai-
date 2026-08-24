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

历史 run 318 和九条 failed queue 不自动重试。
