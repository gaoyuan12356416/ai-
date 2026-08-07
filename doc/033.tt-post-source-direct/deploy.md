# 部署与回滚

## 变更内容

- CPU/GPU 部署同一 GitHub commit。
- GPU：`TT_POST_GPU_MEDIA_MODE=source_direct`。
- CPU：`TT_POST_MEDIA_PROFILE_VERSION=tt-post-source-direct-v1`。
- 两端 `TT_POST_DEFAULT_SOURCE_TRIM_TAIL_SECONDS=0` 保持一致。

## 数据库变更

无 schema/数据迁移。部署前仍创建 SQLite online backup；不得批量修改历史 profile。

## 部署步骤

1. 记录 CPU/GPU 当前 release、服务/unit 状态、非敏感配置、SQLite integrity/status/profile 计数和自然发布基线。
2. 在 `/mnt/data-disk` 创建 CPU/GPU 时间戳备份，保存 SQLite online backup、env/unit 哈希与当前 symlink target。
3. 停止 `tt-post-runner.timer/path` 和 `tt-post-prepare.timer/path`，确认两个 oneshot 均未运行；不取消或重放任何有 `publish_id`/unknown 的任务。
4. 从 GitHub 精确 commit 建立 CPU `/opt/tt-post/releases/<sha>` 和 GPU `/opt/tt-post-gpu/releases/<sha>`，校验源码 SHA。
5. 先切 GPU release 和 mode，重启 `tt-gpu-publisher.service`，检查 `/health`。
6. 再切 CPU release 和 profile，重启 `tt-post-service.service`，检查内部 health/API。
7. 恢复四个 timer/path 原启用状态，确认 `active/waiting`。
8. 观察自然 runner/prepare 日志；不主动触发真实 TikTok 发布。

## 验证

- GPU health：`status=ok`、`media_mode=source_direct`、`profile=tt-post-source-direct-v1`、`direct_post_eligible=true`、`transition=none`、三门禁 ready。
- CPU/GPU service 为 active；reverse tunnel 和 CPU `127.0.0.1:18830` 正常。
- SQLite `PRAGMA integrity_check=ok`；历史 ready/published/unknown 计数无部署性变化。
- 新 profile 下无旧 profile 条目被领取或改写。
- 部署验证不直接调用 TikTok init。

## 回滚

1. 暂停 runner/prepare timer/path并确认 oneshot 空闲。
2. GPU symlink 恢复部署前 release，恢复 `TT_POST_GPU_MEDIA_MODE=direct_outro`，重启 GPU service。
3. CPU symlink 恢复部署前 release，恢复 `TT_POST_MEDIA_PROFILE_VERSION=tt-post-direct-outro-hevc-720x1280-v2`，保持 trim=0，重启 CPU service。
4. 恢复四个 timer/path 并验证 health、profile 和 SQLite integrity。
5. 不删除 `source_direct` 记录或对象；它们在旧 profile 下不会被领取，可供审计或再次切换。

## 注意事项

- 回滚代码和配置必须成对执行，禁止 CPU/GPU profile 不一致。
- 若任何任务已有 `publish_id` 或 unknown 结果，只允许核对，禁止重新 init。

## 2026-08-07 生产记录

- GitHub/生产代码：`202e94b007c6f22efe3c56aaf1e651c188a38856`。
- CPU release：`/opt/tt-post/releases/202e94b007c6f22efe3c56aaf1e651c188a38856`；部署前 release：`/opt/tt-post/releases/4362f3928e8c5c3f437917585b9f645e51986536`。
- GPU release：`/opt/tt-post-gpu/releases/202e94b007c6f22efe3c56aaf1e651c188a38856`；部署前 release：`/opt/tt-post-gpu/releases/f1a0434443751646b848a5d931781ce9a404e511`。
- GitHub archive 两端 SHA256：`d6efac1ca9f7d40935866c3c8f85b70639a61d493b817ae3f0a7ad3476912161`。
- CPU 备份：`/mnt/data-disk/tt-post-publisher/backups/20260807T173347+0800-source-direct-pre-202e94b`。
- GPU 备份：`/data/tt-post-publisher/backups/20260807T173347+0800-source-direct-pre-202e94b`。
- 切换后 CPU health、CPU 到 GPU tunnel health、GPU 本机 health 均通过；GPU 报告 `source_direct` / `tt-post-source-direct-v1` / `direct_post_eligible=true` / `transition=none`。
- 17:37、17:38、17:39 三轮自然 prepare/runner 均为空闲，`publish_request_count=0`；未调用 TikTok init。
- SQLite `integrity_check=ok`；部署后仍为 queue max `91`、run max `95`、`publish_id=89`、active `0`、unknown `0`。

### 精确回滚值

- GPU：把 current 恢复到上述 `f1a0434...` release，并恢复 `TT_POST_GPU_MEDIA_MODE=direct_outro`。
- CPU：把 current 恢复到上述 `4362f39...` release，并恢复 `TT_POST_MEDIA_PROFILE_VERSION=tt-post-direct-outro-hevc-720x1280-v2`。
- 两端继续保持 trim 为 `0`；按“回滚”章节的停调度、GPU 先行、CPU 后行、健康检查、恢复调度顺序执行。
