# 038.TT 自动发布吞吐优化：需求与技术设计

## 背景

2026-08-13 对生产环境过去 12 小时的 2 个自动运行、14 个账号任务做只读分析：素材选择领取平均等待 32 分 49 秒，GPU 制作平均 57 分 21 秒，成片就绪到发布领取平均 37 分 24 秒；TikTok 实际初始化/对账约 49 秒。任务 160–162 在完成 746.9、936.7、1661.5 秒成片后才因账号实时视频时长上限失败，浪费约 80 分钟串行 GPU 时间。

## 目标

1. 超过账号实时最长视频限制的素材不进入 GPU 制作。
2. 自动任务可提前制作，但不得早于冻结的 `scheduled_at_utc` 发布。
3. 长制作期间始终保留发布/对账执行能力。
4. GPU 返回可审计的分阶段耗时，写入任务事件。

## 范围

### 包含

- TT 自动发布的调度、任务领取、素材选择、GPU 准备和 runner。
- 新增非敏感配置 `TT_AUTO_POST_PREPARE_AHEAD_SECONDS`、`TT_AUTO_POST_PUBLISH_POLL_SECONDS`。
- GPU 阶段：素材快照、下载、源探测、GPU 排队、转码、输出校验、上传、总耗时。

### 不包含

- 不改变 TikTok 发布 payload、账号设置或历史素材永久去重语义。
- 不手动发真实 TikTok 作为部署验证。
- 本次不修改 FFmpeg 画质参数、不启用第二路 GPU 转码；待生产阶段数据和离线基准证明安全后另立需求。

## 业务规则

- 自动运行最多提前 12 小时创建；生产配置先采用 4 小时。
- `pending` 可在准备窗口内领取；`ready` 只有 `scheduled_at_utc <= now` 才可领取发布。
- 4 个执行槽中，3 个只允许 `selection/prepare`，1 个只允许 `publish/reconcile`。
- 选材前调用账号实时 Creator Info，最终素材时长上限为 `min(模板上限, 账号实时上限)`；发布前仍二次校验。
- Creator Info 失败按原选择阶段瞬态失败规则重试，不退化为默认上限。

## 技术设计

### 影响模块

- `features/tt_auto_posts/core.py`：准备窗口、阶段限定领取、ready 发布时间闸门、事件详情。
- `features/tt_auto_posts/publisher.py`：Creator Info 预检、有效时长规则、GPU 阶段事件。
- `features/tt_auto_posts/service.py`：提前调度、内部 phases 参数、健康信息。
- `scripts/tt_auto_post_runner.py`：3 个制作槽 + 1 个发布保留槽。
- `features/tt_gpu/worker.py`：阶段计时并固化到 manifest。

### 数据结构

不新增 SQLite 列和表。`tt_auto_event.details_json` 增加：

```json
{"gpu_reused":false,"stage_timings_ms":{"download":1234,"gpu_queue_wait":456,"transcode":7890,"total":9999}}
```

### 接口

内部 `POST /internal/tt-auto-post/execute-next` 保持 `worker_id` 兼容，可选 `phases` 为阶段数组。GPU `/prepare` 响应兼容增加 `stage_timings_ms`。

## 验收标准

1. 账号上限 60 秒、模板上限 120 秒时，选择请求冻结的上限为 60 秒且未调用 GPU prepare。
2. 提前创建的任务可以选材/制作；发布时间前发布 lane 返回无任务，到点后方可领取。
3. runner 的制作 worker 不领取发布，发布 worker 不领取制作。
4. 新旧 GPU manifest 均可读取；新 manifest 返回 8 个有界非负计时字段。
5. TT auto、TT Post、主应用契约回归通过。

## 风险与控制

- Creator Info 增加选择阶段外部调用：失败关闭并进入 5 分钟重试，不使用陈旧或默认限制。
- 提前制作会增加本地成片驻留时间：沿用现有本地容量门禁和发布终态清理。
- runner 阶段拆分可能饿死某类任务：保留 `worker_count=1` 兼容路径，阶段过滤在同一 SQLite 事务内完成。
- 回滚为切回上一 release 并移除两个新增非敏感配置；无数据库回滚。

## 变更记录

- 2026-08-13：初稿、SA/测试评审后进入实现。
