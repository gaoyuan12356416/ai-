# X 短剧完整重发

## 业务范围

管理员明确要求“完全重发”时，只允许对已有完整、确定发布历史的短剧开启一个新的重发代次。旧队列、发布日志、X Post ID 和链接必须原样保留，不删除、不改写。

本能力只重置短剧池进度，不立即建队列、不补跑已经过去的时间点，也不直接向 X 发帖。新代次由后续自然定时点从 Episode 1 开始发布，并按既有的一剧一号规则重新绑定账号。

## 数据契约

- `x_post_drama_pool.replay_generation`：当前发布代次，初始为 1。
- `x_post_queue.drama_replay_generation`：队列所属代次；历史 drama 队列迁移为 1。
- `x_post_drama_replay_audit`：追加式审计表，记录重置前的剧、进度、归属、操作人和策略原因，禁止更新和删除。
- 第一代 Episode Key 保持 `content_id:episode`，兼容历史。
- 第二代及以后使用 `content_id:replayN:episode`，允许同一剧重新发布 Episode 1，同时保持跨代全局排重。

## 生效门禁

重发重置必须同时满足：

1. 没有 `claimed`、`queued` 或 `running` 的短剧定时批次。
2. 每个目标剧的当前快照与管理员确认的 `content_id/status/generation/free/published/next/account` 完全一致。
3. 当前代全部历史队列和日志均为 `published`，没有未知结果，并都有 X Post ID 和链接。
4. 当前代 Episode 必须从 1 连续到 `published_episode_count`，且 `next_sub_number=published_episode_count+1`。
5. 操作原因固定为 `operator_full_replay_v1`。

任意目标不满足时，整批回滚，不能部分重置。

## 运维命令

脚本 `scripts/x_post_drama_replay_reset.py` 默认只校验：

```bash
python3 scripts/x_post_drama_replay_reset.py \
  --pool-id 2 \
  --expected-snapshots /secure/replay-snapshots.json \
  --actor-user-id USER_ID \
  --actor-name USER_NAME
```

正式生效还必须增加：

```bash
--apply \
--confirm operator_full_replay_v1 \
--report-path /mnt/data-disk/x-post-automation/replays/NEW_REPORT.json
```

正式生效必须持有既有发布锁并生成新的 JSON 审计报告。脚本本身不会创建发布队列或发帖。

## 部署与回滚

1. 部署前做 SQLite 在线备份，并在副本上完成 schema 迁移、重发 dry-run、apply 和 `PRAGMA integrity_check` 演练。
2. 从 GitHub 精确 commit 创建不可变 release，再切换 sidecar 和主 API 所需文件。
3. 生产先 dry-run，再 apply 重置，最后保存新的短剧账号与发布时间配置。
4. 回读审计行、池代次、旧队列/日志计数、新配置和 timer 状态；不手工触发发帖。
5. 若新代次尚未产生任何队列，可在停用短剧定时后评估数据库备份回滚；一旦已有新队列或 Post，禁止恢复旧数据库，应保留新历史并前向修复。
