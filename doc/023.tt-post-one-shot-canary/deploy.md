# 部署文档

## 变更内容

- GPU 新增独立 `/internal/tt-post/canary-publish`。
- CPU 新增精确的一次性人工私密许可与页面状态。
- TikTok 错误保真。
- 正式发布与每日排期门禁保持不变。

## 配置项

两端共同：

```text
TT_POST_MANUAL_CANARY_ENABLED=1
TT_POST_MANUAL_CANARY_ACKNOWLEDGEMENT=I_ACCEPT_ONE_SHOT_PRIVATE_TIKTOK_CANARY_20260731
TT_POST_MANUAL_CANARY_ID=tt640-m5391678-20260731
TT_POST_MANUAL_CANARY_EXPIRES_AT_UTC=2026-07-31T09:30:00Z
TT_POST_MANUAL_CANARY_ACCOUNT_ID=640
TT_POST_MANUAL_CANARY_MATERIAL_ID=5391678
TT_POST_MANUAL_CANARY_CONTENT_ID=F59JjB15bc
TT_POST_MANUAL_CANARY_GPU_JOB_ID=ttpreview-53f01e288fc0c6297f5fa709b98cdce332c7
TT_POST_MANUAL_CANARY_OUTPUT_SHA256=5c453d5c2e3e600bc9307feaaccfe630ee16ef393d8cbee7efd2f6c741bf458f
TT_POST_MANUAL_CANARY_OUTPUT_SIZE=195521735
TT_POST_MANUAL_CANARY_PROFILE=tt-post-hevc-720x1280-v2
```

CPU 额外：

```text
TT_POST_MANUAL_CANARY_POOL_ID=1
```

GPU 额外：

```text
TT_POST_MANUAL_CANARY_ORIGIN=https://advertising-1306474899.cos.ap-hongkong.myqcloud.com
```

以下正式门禁在 CPU/GPU 均保持：

```text
TT_POST_LIVE_ENABLED=0
TT_POST_DIRECT_AUDIT_APPROVED=0
TT_POST_URL_PROPERTY_VERIFIED=0
```

## 数据库变更

无 schema 变更。部署前备份 CPU SQLite；GPU manifest 与 publish ledger 不修改/不清理。

## 部署步骤

1. 合并并推送 GitHub commit。
2. GPU 拉取精确 commit，创建不可变 release，更新环境变量，重启并验证 health。
3. CPU 拉取同一精确 commit，创建不可变 release，更新环境变量，重启 sidecar。
4. 确认 runner/timer active，但 schedule 数为 0。
5. 确认页面仅目标账号显示“仅一次私密测试”，每日排期不可编辑。
6. 确认队列、schedule run、publish ID、GPU ledger 仍为 0；不代用户点击。

## 验证步骤

- CPU `/health` 正常、正式 gates 为 false。
- GPU `/health` 正常、正式 gates 为 false、`manual_canary.active=true`。
- 目标 schedule API：`manual_canary_ready=true`、`can_publish_now=true`。
- 其他账号：`manual_canary_ready=false`。
- SQLite：schedule/queue/run/publish ID 基线未变化。
- GPU：目标 publish ledger 在用户点击前不存在。

## 回滚方案

- 将 CPU/GPU `current` 链接回切至各自备份 release。
- 恢复两端环境文件备份并重启服务。
- 回滚后确认三重门禁仍为 0、队列状态未被改写。
- 若用户已点击并产生 `publish_id` 或 unknown，代码回滚不能重新 init；只能走 reconcile/人工核对。

## 注意事项

- 用户应在 2026-07-31 17:30（Asia/Shanghai）前测试；过期后许可自动 inactive。
- 测试结束后将 `TT_POST_MANUAL_CANARY_ENABLED=0` 并重启两端。
- 不得为了测试设置 `TT_POST_DIRECT_AUDIT_APPROVED=1` 或 `TT_POST_URL_PROPERTY_VERIFIED=1`。
