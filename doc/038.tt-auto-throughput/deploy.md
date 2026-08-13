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

## 2026-08-13 生产记录

- GitHub/runtime commit：`9425b39fa45390b3dc107f353dc6ef436415365d`。
- CPU/GPU release：
  - `/opt/tt-auto-post/releases/9425b39fa45390b3dc107f353dc6ef436415365d`
  - `/opt/tt-post-gpu/releases/9425b39fa45390b3dc107f353dc6ef436415365d`
- 候选归档 SHA256：`83be128f1f38ea10d668dba47e50fca6fcdea8d0c2db8b326b735a336a8f81f7`，CPU/GPU 一致；CPU `git ls-remote` 返回相同 commit。
- 部署前等待旧任务 168 完成 FFmpeg 和对象存储上传；只暂停 scheduler/runner timer/path，未终止旧 runner 或 GPU worker。
- CPU 备份：`/mnt/data-disk/tt-auto-post-deploy/backups/20260813T111307+0800-throughput-pre-9425b39`，SQLite online backup `quick_check=ok`，部署前 CPU release 为 `8df092b80cad6737dc11af375f27da13ea8bf234`。
- GPU 备份：`/data/tt-post-publisher/backups/20260813T111307+0800-throughput-pre-9425b39`，部署前 GPU release 为 `d3202fc829379fce91de6ffa4588cd29af36492e`。
- GPU 先切换，random worker 更新；独立 direct-outro worker PID 未随之重启。GPU health 保持 `random_overlay`、v3、asset identity ready、gates ready。
- CPU health 返回 `prepare_ahead_seconds=14400`，两条 frozen video-template route 正确；service、scheduler timer、runner timer/path 全部 active，SQLite `quick_check=ok`。
- 未执行 run-now、内部 execute-next 或人为 TikTok canary；全部发布验证来自 11:17 后自然 timer。
- 自然 run 30：任务 166 在 11:17:00 被 publish lane 领取，11:17:18 发布；与此同时任务 170–172 进入 selection，证明长制作不再阻塞 ready 发布。
- 任务 170–172 的冻结素材时长/账号有效上限分别为 `1719/3600`、`304/3600`、`1419/3600` 秒，证明 Creator Info 预检在 GPU 前生效。
- 自然任务 172 于 11:53:13 ready，同秒被 publish lane 领取，11:53:48 发布成功。GPU 阶段耗时：asset snapshot `318ms`、download `14971ms`、source probe `30ms`、GPU queue `0ms`、transcode `1624042ms`、output verify `572ms`、upload `364712ms`、total `2004644ms`。
- 该样本中转码占总时长约 `81.0%`，上传约 `18.2%`。后续优先做离线滤镜/CPU 基准，再评估上传链路；本次未提高 GPU 并发、未改变画质参数。

精确常规回滚（不恢复 SQLite）：

```bash
systemctl stop tt-auto-post-runner.path tt-auto-post-runner.timer tt-auto-post-scheduler.timer
systemctl stop tt-auto-post-service.service
cp -a /mnt/data-disk/tt-auto-post-deploy/backups/20260813T111307+0800-throughput-pre-9425b39/tt-auto-post.env /etc/tt-auto-post.env
ln -s /opt/tt-auto-post/releases/8df092b80cad6737dc11af375f27da13ea8bf234 /opt/tt-auto-post/.current-throughput-rollback
mv -Tf /opt/tt-auto-post/.current-throughput-rollback /opt/tt-auto-post/current
systemctl start tt-auto-post-service.service

# GPU 仅在确认无 FFmpeg/prepare in-flight 后执行：
systemctl stop tt-gpu-publisher.service
ln -s /opt/tt-post-gpu/releases/d3202fc829379fce91de6ffa4588cd29af36492e /opt/tt-post-gpu/.current-throughput-rollback
mv -Tf /opt/tt-post-gpu/.current-throughput-rollback /opt/tt-post-gpu/current
systemctl start tt-gpu-publisher.service

systemctl start tt-auto-post-scheduler.timer tt-auto-post-runner.timer tt-auto-post-runner.path
```
