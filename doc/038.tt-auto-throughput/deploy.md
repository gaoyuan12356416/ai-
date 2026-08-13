# 部署文档

## 变更内容

- CPU：TT auto core/publisher/service/runner。
- GPU：worker 阶段计时；无 FFmpeg 参数变化。
- 配置：生产启用 `TT_AUTO_POST_PREPARE_AHEAD_SECONDS=14400`，poll 默认 15 秒。
- 数据库：无 schema 迁移。

## 部署前

1. 确认 `/mnt/data-disk` 和 GPU `/data` 仍为独立挂载且容量正常。
2. 等待当前 `tt-auto-post-runner.service`、GPU FFmpeg 自然结束。
3. 备份 CPU/GPU current 指针、`/etc/tt-auto-post.env`、TT auto SQLite。
4. 从 GitHub 精确 commit 建立 immutable release。

## 部署顺序

1. GPU 切换 release，重启 `tt-gpu-publisher.service`，health 验证；不影响 direct-outro 独立 worker。
2. CPU 切换 release并写入提前窗口配置，重启 `tt-auto-post-service.service`。
3. 不手动启动真实任务；等待 systemd scheduler/runner 的自然周期。

## 验证

- CPU/GPU health 与 release commit 一致。
- 离线/内部只读检查确认 health 暴露 14400。
- 下一次自然任务：`selected_at_utc < scheduled_at_utc`、`prepared_at_utc` 可早于发布时间、`published_at_utc >= scheduled_at_utc`。
- `task_preparation_ready.details_json.stage_timings_ms` 有值。
- 超过账号上限的素材不被保留/制作。

## 回滚

1. 停止正在启动但尚未进入 TikTok publish 的新 runner（若有）；不触碰有 publish evidence 的任务。
2. CPU/GPU current 分别切回部署前 release。
3. 恢复 `/etc/tt-auto-post.env` 备份并重启相应 sidecar。
4. SQLite 无 schema 变化，通常无需恢复；仅在文件损坏时使用部署前备份。
