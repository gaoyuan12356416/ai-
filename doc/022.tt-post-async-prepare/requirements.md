# 022.tt-post-async-prepare 需求与技术设计

## 背景

当前“批量填写素材 ID”会在校验阶段同步调用 GPU 完成下载、去尾、拼接片尾、转码与成片上传。单条长视频可能耗时数分钟，页面长期停在“读取中 1/1”，既无法及时告诉操作人素材是否存在，也会让浏览器请求超时或被误判为系统卡死。

## 目标

把“素材识别”和“成片预制作”拆为两个阶段：

1. 页面读取素材时只访问只读素材库，校验素材存在并解析真实 `content_id`、源视频地址及基础信息。
2. 校验通过后允许立即加入持久化素材池，接口不等待 GPU。
3. 独立后台 runner 按账号严格 FIFO 异步下载、去尾、拼片尾、压缩并校验成片。
4. 只有预制作状态为 `ready` 的素材才进入既有 `tt_post_recurring_pool`，可被每日时点或手动发布消费。
5. 页面展示等待制作、制作中、等待重试、制作完成、制作失败状态，操作人无需保持页面打开。

## 范围

### 包含

- `/materials/preview` 与兼容入口 `/materials/prepare` 改为快速只读校验。
- 新增 additive `tt_post_material_intake` 入池表，不放宽既有 ready pool 的完整性约束。
- 素材入池幂等、跨池去重、冻结账号/Drama ID/文案/制作版本/去尾参数。
- 独立 `tt_post_prepare_runner.py`、systemd service/path/timer。
- durable claim、租约续期、fencing token、过期租约回收、有限自动重试。
- 合并返回 intake 与 ready pool 的素材池查询及前端状态轮询。
- 生产备份、部署、回滚与不触发真实 TikTok 发布的验证流程。

### 不包含

- 不修改 TikTok Direct Post、每日发布时点、手动发布、发布对账的业务语义。
- 不开放生产发布安全闸门，不以真实 TikTok Post 作为上线验证。
- 不迁移 GPU 存储后端，不改变 COS/本地发布方案。
- 本需求不提供前端手动重试或取消按钮；失败记录由后续运维流程处理。

## 用户故事与业务规则

1. 作为操作人，我输入一个或多个素材 ID，点击“批量校验”后应快速知道每条素材是否存在、真实 Drama ID 是什么；校验过程不得等待视频制作。
2. 校验通过的素材可立即入池。成功提示应明确“已入池，后台预制作”，不得显示为“已可发布”。
3. 同一批次可部分成功；错误素材不阻塞其他素材入池。
4. 同一 `idempotency_key` 加相同冻结请求重复提交，返回原记录；同键不同请求返回 `409`。
5. 同一素材不得同时存在于 intake、ready pool、旧素材池或发布队列；相同请求重放除外。
6. 每个账号内按 `created_at,id` 严格 FIFO：前一条处于 `queued/preparing/retry_wait` 时，后一条不得越过；不同账号可按全局最早的“账号队首”依次处理。
7. `queued`、`preparing`、`retry_wait`、`failed` 均不可发布；只有 `ready` 且 ready pool 状态为 `available` 才可被调度。
8. 预制作前重新校验账号、已保存发布设置及 Creator Info；成片时长必须满足账号实时限制。
9. 临时性 5xx/非终态错误最多尝试 5 次，采用指数退避并加入确定性抖动；明确的素材、身份、成片元数据或账号限制错误直接进入 `failed`。
10. runner 崩溃或 CPU 服务重启后，租约到期的 `preparing` 可被重新领取；旧 claim token 不得续租、完成或覆盖新 owner 的结果。

## 交互与流程

### 主流程

1. 输入素材 ID。
2. 前端逐条调用 `POST /api/admin/tt-posts/materials/preview`。
3. API 只查素材库并返回 `status=validated`、`preparation_status=not_started`、`publish_ready=false`。
4. 操作人选择 TikTok 账号、确认文案和授权，点击“加入素材池”。
5. API 再次解析素材并核对页面提交的 `content_id`，在一个 SQLite 事务内写入 `tt_post_material_intake(status=queued)`，触发 `/run/tt-post/prepare-kick` 后立即返回。
6. path unit 尽快唤醒 prepare runner；timer 每分钟兜底。
7. runner 使用 loopback 内部接口领取一条账号队首记录，周期性续租，并请求 sidecar 执行 GPU prepare。
8. sidecar 校验 GPU 成片的 job/content/profile/HTTPS URL/SHA256/大小/时长，再在一个事务内写入 `tt_post_recurring_pool(status=available)` 并把 intake 置为 `ready`。
9. 前端轮询素材池列表，显示最新制作状态；调度器只读取 ready pool。

### 状态机

`queued -> preparing -> ready`

可重试错误：`preparing -> retry_wait -> preparing`

终态错误：`preparing -> failed`

租约过期：`preparing(lease expired) -> preparing(new claim token)`

预留终态：`queued/retry_wait -> canceled`（本期无前端入口）。

## 技术设计

### 影响模块

- `features/tt_posts/core.py`：intake 表、事务、幂等、FIFO claim、lease/fencing、原子完成与失败状态。
- `features/tt_posts/service.py`：快速 preview、快速入池、素材池合并列表、内部 prepare 状态机接口。
- `scripts/tt_post_prepare_runner.py`：独立 one-shot runner、进程锁、租约心跳、严格 loopback client。
- `deploy/tt-post-prepare.{service,path,timer}`：独立运行单元与唤醒/兜底。
- `deploy/tt-post.env.example`：prepare runner 配置及单位约束。
- `static/tt-post-pool.html`：校验文案、入池文案、状态表与轮询。
- `scripts/test_tt_posts_core.py`、`scripts/test_tt_posts_service.py`、`scripts/test_tt_post_prepare_runner.py`、`scripts/test_tt_post_pool_ui.py`：自动化验证。

### 数据结构

新增 `tt_post_material_intake`：

- 业务身份：`material_id`、`account_id`、`content_id`、`source_media_url`。
- 冻结制作合同：`gpu_job_id`、`source_trim_tail_seconds`、`preparation_profile`。
- 冻结发布合同：`caption_template`、`caption`、`consent_version`、`consented_at_utc`、`is_aigc`。
- 幂等：`idempotency_key UNIQUE`、`request_sha256`、`material_id UNIQUE`、`gpu_job_id UNIQUE`。
- 状态：`queued/preparing/retry_wait/ready/failed/canceled`。
- lease/fencing：`claim_worker`、`claim_token`、`lease_expires_at_utc`、`attempt_count`、`next_attempt_at_utc`。
- 结果：成片 URL/SHA256/大小/时长、`recurring_pool_id UNIQUE`。
- 诊断：安全裁剪后的 `error_code/error_message` 及各阶段时间。

既有 `tt_post_recurring_pool` 不改字段语义，继续只保存完整可发布成片。完成 intake 与插入 ready pool 必须处于同一事务，避免 ready 状态与 pool 行分裂。

### API

- 公共管理接口保持原路径：
  - `POST /api/admin/tt-posts/materials/preview`
  - `POST /api/admin/tt-posts/materials/prepare`（兼容别名，同样只校验）
  - `POST /api/admin/tt-posts/material-pool`
  - `GET /api/admin/tt-posts/material-pool`
- 新增仅 loopback + 独立 bearer 的内部接口：
  - `POST /internal/tt-posts/preparations/claim`
  - `POST /internal/tt-posts/preparations/{id}/renew`
  - `POST /internal/tt-posts/preparations/{id}/process`

### 配置与单位

- `TT_POST_PREPARE_LEASE_SECONDS=180`，单位秒，范围 60–600。
- `TT_POST_PREPARE_RENEW_INTERVAL_SECONDS=30`，单位秒，范围 5–600，且三倍续租间隔不得大于 lease。
- `TT_POST_PREPARE_INTERNAL_TIMEOUT=60`，单位秒，普通内部请求超时。
- `TT_POST_GPU_PREPARE_TIMEOUT=9000`，单位秒，GPU prepare 上限。
- `TT_POST_PREPARE_PROCESS_TIMEOUT=9300`，单位秒，必须至少比 GPU prepare timeout 多 60 秒。
- `tt-post-prepare.service TimeoutStartSec=9600s`，必须大于 process timeout。
- `TT_POST_PREPARE_RUNNER_LOCK_PATH=/run/tt-post/prepare-runner.lock`。
- `TT_POST_PREPARATION_KICK_PATH=/run/tt-post/prepare-kick`。

### 异常与边界

- 校验失败：只返回该素材错误，不创建 intake。
- 入池后 kick 写入失败：入池仍成功，由一分钟 timer 兜底。
- 页面关闭/刷新：状态保存在 SQLite，后台继续处理。
- GPU 慢：HTTP 长调用由独立 runner 承担，不占用发布 runner；lease 心跳持续续期。
- runner 并发启动：文件锁保证同机只有一个；数据库 claim 再提供事务级互斥。
- 旧 token：使用常量时间比较且必须匹配未过期 `preparing` 行，防止 ABA/过期 owner 完成。
- 完成阶段发现素材已进入其他池/队列：失败关闭，不覆盖历史。
- 日志和 API 不返回 claim token、lease 细节或凭据；错误信息经过裁剪。

## 验收标准

1. preview 的测试替身 GPU 调用次数为 0，能返回真实素材与 Drama ID。
2. 入池接口在 GPU 未执行时返回 `queued`，且 `publish_ready=false`。
3. 后台 runner 完成后，同一 intake 变为 `ready`，ready pool 新增且可用计数加 1。
4. `queued/preparing/retry_wait/failed` 不会被每日调度或“立即发布”消费。
5. 同账号 FIFO、跨账号队首选择、租约过期恢复和旧 token fencing 均有自动化测试。
6. 重复同请求不重复制作/入池；幂等键或素材冻结信息冲突返回 `409`。
7. prepare runner 与既有 publish runner 使用不同 service、lock、path、timer，长制作不延误到点发布。
8. 页面在入池后立即恢复操作，持续显示制作状态；轮询不触发制作请求。
9. 数据库升级只新增表/索引，不重建或放宽既有 ready pool。
10. 部署验证保持三个生产发布 gate 关闭，不发真实 TikTok Post。

## 风险与待确认

- 首条同账号素材进入长期 `retry_wait` 会按严格 FIFO 阻塞该账号后续素材，这是有意的顺序保证；需要运维关注 `failed/retry_wait`。
- GPU prepare 最长可达 9000 秒，systemd 与代理超时必须按秒一致，禁止误填毫秒。
- 本期没有前端“重试失败项”按钮；若业务需要人工恢复，另立需求并保留审计。
- 素材库解析仍依赖远端只读 MySQL；其短时故障会使校验失败，但不会影响已入池记录。

## 变更记录

| 日期 | 内容 |
| --- | --- |
| 2026-07-30 | 首版：拆分快速校验、durable intake 与独立后台预制作。 |
