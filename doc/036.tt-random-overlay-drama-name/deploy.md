# 部署文档

## 变更内容

- CPU 素材池和自动发布：剧名宏、新 profile、trim=0。
- GPU：`random_overlay` 模式、版本资产集及确定性配方。
- 保留 `source_direct` 与 `direct_outro` 代码和原配置回滚值。

## 配置项

```text
TT_POST_GPU_MEDIA_MODE=random_overlay
TT_POST_GPU_RANDOM_OVERLAY_ROOT=/data/tt-post-publisher/random-overlay-assets/v1
TT_POST_GPU_RANDOM_OVERLAY_MANIFEST_SHA256=028326ab211418934b026c227f2e3707553cce7560551dca3c0bfddc681d566f
TT_POST_MEDIA_PROFILE_VERSION=tt-post-random-overlay-hevc-720x1280-v1
TT_POST_DEFAULT_SOURCE_TRIM_TAIL_SECONDS=0
```

## 部署步骤

1. GitHub 推送并锁定 exact commit。
2. CPU/GPU 分别备份 env、unit、current release；在线备份两套 SQLite，记录 publish_id 基线。
3. 验证 CPU `/mnt/data-disk` UUID 和 GPU `/data` 挂载/空间。
4. 上传源模板到 GPU 数据盘临时目录，运行资产构建器，核对 manifest/全部 SHA，再原子改名为版本目录。
5. 在候选 release 运行测试和离线 prepare；不得调用 TikTok publish。
6. 暂停两套 scheduler/runner/path，确认 oneshot 与 GPU prepare 均无 in-flight。
7. 先切 GPU release/env 并验证 health，再切两个 CPU release/env；三方 profile/trim 完全一致后恢复 timers/paths。
8. 观察自然 scheduler/runner 和 ledger，不创建测试发布任务。

## 回滚

- 暂停调度后把三方配置一起切回 `source_direct` + `tt-post-source-direct-v1` + trim 0，或切回旧 `direct_outro` 配对配置。
- 只切代码/env，不恢复包含后续发布事实的 SQLite、GPU manifest 或 publish ledger。
- 资产版本目录保留，确认无引用后另行清理，不纳入紧急回滚。

## 注意事项

- 自动发布生产门禁当前为开启状态；切换窗口必须先确认无运行中任务。
- 验收不运行真实发帖 canary。
