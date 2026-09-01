# 部署文档

## 变更内容

短剧池新队列按最终成片时长解析 direct/relay/waiting；加法 companion route table 与 DB 围栏；列表/剧集详情展示路线和最终时长。

## 配置项

`X_POST_DRAMA_DURATION_ROUTING_ENABLED=false|true`，默认 false。CPU scheduler 与 Sidecar 必须读取相同值。既有 GPU repair 配置不变。

## 数据库变更

仅 `CREATE TABLE/INDEX/TRIGGER` 加法迁移，不重建/删除/复制 `x_post_queue`。迁移前后记录表计数、schema hash、`quick_check`、`foreign_key_check`、历史 141 与 unknown 摘要。

## 部署步骤

1. 验证本地/远端不可变 commit 与干净工作树。
2. 验证数据盘 UUID `3e8ac4e8-7770-456d-9e89-2ec5dd405fa8`、可写、空间充足。
3. 记录所有共享 SQLite writer 的原始状态：schedule/claim/manual timers 与 oneshot、`x-auto-post-runner.timer`/oneshot、Sidecar、Main 人工发布入口；在线备份 SQLite，并备份 service units、env 权限/哈希、Main composite 文件和 current release。
4. 停止全部同库 timers，等待 oneshot 排空；短维护停止 Sidecar/Main 写入口，确认无相关运行进程、无 publishing/unknown 新增后再迁移。不得只停短剧 scheduler 而遗漏素材/人工/X Auto writer。
5. feature-off 创建不可变 release，运行 additive schema 初始化；在 writer 仍停止时复核 quick/FK、表计数、schema hash 与备份摘要。
6. Sidecar/Main 共享 `service.py`、Sidecar `oauth_service.py`、scheduler 统一同一 commit；不得整体覆盖 Main OAuth composite。
7. 重启 Sidecar/Main，保持全部共享 SQLite timers 停止，仅完成 feature-off health、schema 与兼容性检查；feature-off 期间禁止运行任何自然 drama slot，避免旧 141 路线产生新队列。
8. 再次短维护停止写入口，在 Sidecar 与 scheduler 两份环境中同时设置 feature=true，重启相关服务并核对两端配置摘要一致；只有此后才按原 active 状态恢复全部 timers，并只观察自然新队列。

## 验证步骤

- 服务 active/enabled、health 200、timer 正常、无 crash loop。
- SQLite quick_check=ok、FK=0；历史队列/141/unknown 摘要不变。
- 新 drama queue 有 companion；pending/waiting 零 publish log/repost ledger。
- 素材/人工/X Auto 无新状态。
- 首个自然短 direct 与长 relay 分别完成 queue-ledger-platform 对账才算业务验收。

## 回滚方案

1. 先停止全部同库 timers（含 X Auto）并排空 oneshot，短维护停止 Sidecar/Main 写入口，再关闭 Sidecar 与 scheduler 两端 feature flag；禁止在 feature-off 与 timer 恢复之间运行自然 drama slot。
2. 保持兼容 release 停放现有 pending/waiting；不得强行发布、换剧或改路。
3. 只有 pending/waiting=0 且无 publishing/unknown 时才允许切旧 release。
4. 恢复 unit/env/current symlink，重启并复核 health/timers/quick/FK。
5. 加法表与已发布账本保留；发布恢复后禁止整体回滚 SQLite。

## 注意事项

全程禁止真实测试 Post/Repost。健康、mock 或 worker activity 不等于外部平台发布成功；自然排期证据需单独对账。
