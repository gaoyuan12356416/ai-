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

## 2026-07-31 生产部署记录

- GitHub 分支：`codex/tt-post-one-shot-canary-20260731`
- 部署代码 commit：`339becc54893529fbec05e93ac25f727aea0f25f`
- CPU release：`/opt/tt-post/releases/339becc54893529fbec05e93ac25f727aea0f25f`
- GPU release：`/opt/tt-post-gpu/releases/339becc54893529fbec05e93ac25f727aea0f25f`
- CPU 成功切换：2026-07-31 11:44:21 CST
- GPU 成功切换：2026-07-31 11:39:13 CST
- CPU 回滚点：
  `/mnt/data-disk/tt-post-publisher/backups/tt-post-one-shot-canary-20260731T034414Z`
- GPU 回滚点：
  `/data/tt-post-publisher/backups/tt-post-one-shot-canary-20260731T033912Z`
- CPU/GPU 精确 release 均执行 220/220 回归并通过。
- 首次 GPU 健康断言使用了错误的响应字段结构，自动回切到
  `/opt/tt-post-gpu/releases/af3ae7b`；修正验收脚本后重新部署成功。
  该次安全回滚备份为
  `/data/tt-post-publisher/backups/tt-post-one-shot-canary-20260731T033741Z`。
- 首次 CPU API 验收错误地检查了不存在的 `manual_canary.active`；
  自动回切到 `/opt/tt-post/releases/bb9024ba7b7c`。按公开 DTO 的
  `enabled/ready` 字段修正后重新部署成功。该次安全回滚备份为
  `/mnt/data-disk/tt-post-publisher/backups/tt-post-one-shot-canary-20260731T034215Z`。

### 上线后只读验收

- CPU/GPU 服务 active；GPU 反向隧道 active。
- 两端三个正式 Direct Post gate 均为 false；GPU 正式 `ready=false`。
- GPU `manual_canary.active=true`、`privacy_level=SELF_ONLY`。
- 账号 640：
  `manual_canary_ready=true`、`can_publish_now=true`、待发素材 1 条。
- 账号 641：
  `manual_canary_ready=false`、`can_publish_now=false`。
- TikTok 实时 creator-info：
  `@dramawave996`，支持 `SELF_ONLY`，最长视频 3600 秒。
- SQLite `integrity_check=ok`；daily schedule、queue、schedule run、
  event、publish ID 均为 0；pool 1 仍为 `available`。
- 11:44、11:45、11:46、11:47 四次自然 runner：
  `schedule_due_count=0`、`publish_request_count=0`。
- GPU 目标 publish ledger 不存在，journal 没有 `/video/init/` 或
  `publish_id`。
- 三份 `tt-post-pool.html` SHA-256 均为
  `6b23994e1821368aaa202404ee5411e6b89d53b50310943bc075a96286f94c26`；
  公网响应 200，并带 `no-cache, no-store, must-revalidate`。
- 浏览器刷新后确认新 HTML 已包含“仅一次私密测试”、
  `manualCanaryReady` 和“强制 SELF_ONLY”的前端合同。刷新时现有登录会话
  要求重新登录，因此最终登录态按钮由用户重新登录/刷新后亲自确认和点击。
- 部署和验收均未调用 `run-now` 或 TikTok 发布接口。
- 不得为了测试设置 `TT_POST_DIRECT_AUDIT_APPROVED=1` 或 `TT_POST_URL_PROPERTY_VERIFIED=1`。
