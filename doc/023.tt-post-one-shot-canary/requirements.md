# 023.tt-post-one-shot-canary 需求与技术设计

## 背景

TikTok Post 正式发布门禁仍全部关闭，现网每日排期为 0。运营需要对账号 640、素材 5391678 做一次真实发布测试，取得 TikTok 原始返回后再决定后续调整。

## 目标

- 只允许指定账号与指定 GPU 成片执行一次人工发布。
- 强制使用 `SELF_ONLY`，关闭评论、Duet、Stitch 与商业内容开关。
- 每日自动排期继续关闭，任何其他账号、素材或自动任务继续受三重正式门禁阻断。
- 不伪造 Direct Post 审核通过或 URL Property 已验证状态。
- TikTok 初始化请求一旦发出，无论成功、明确拒绝或结果未知，都不得自动重试。
- UI 只负责让用户亲自点击，部署和验证不得代用户触发发布。

## 范围

### 包含

- CPU 发布服务的一次性人工测试白名单。
- GPU 发布服务的同一账号、Job、Drama ID 二次校验。
- 人工测试队列强制私密发布。
- 页面按钮状态与一次性测试提示。
- 回归测试、CPU/GPU 灰度部署、回滚与现场只读验证。

### 不包含

- 开启每日自动发布。
- 将正式门禁改为已通过。
- 代用户点击“立即发布一条”。
- 保证 TikTok 一定接受发布；本次目标之一就是保留真实错误。
- 修改或重新制作现有成片。

## 业务规则

1. 只有 `trigger_type=manual` 的运行可使用测试白名单。
2. CPU 必须同时匹配账号、素材 ID、GPU Job ID；GPU 必须同时匹配账号、Job ID、Drama ID。
3. 测试请求强制 `SELF_ONLY`，评论、Duet、Stitch、品牌内容与品牌自然内容开关均为关闭。
4. 自动触发即使匹配账号也不得绕过正式门禁。
5. GPU 仍校验成片来自当前配置的存储源；仅跳过“正式门禁已批准”和旧 manifest 的 `direct_post_eligible=false` 阻断。
6. GPU 在向 TikTok 发起初始化前写入幂等账本；明确拒绝、未知结果和成功均保留终态，禁止再次初始化。
7. CPU 队列进入任一终态后，对应素材从本次发布池消耗，runner 不会再次领取。
8. 白名单默认为关闭；启用必须同时提供固定确认短语和完整目标字段。

## 交互流程

1. 用户选择账号 640。
2. 页面读取服务端状态；只有指定素材仍为下一条可用素材时，显示“一次性私密测试已开放”并启用按钮。
3. 用户点击“立即发布一条”。
4. CPU 冻结队列并强制私密策略，runner 领取后向 GPU 发起带一次性标记的请求。
5. GPU 再次核对目标与成片来源，写入幂等账本，然后调用 TikTok。
6. 页面任务列表展示 TikTok 原始错误码/信息，或展示待核对的 `publish_id`。

## 技术设计

### 影响模块

- `features/tt_posts/core.py`：按队列只读查询对应每日发布运行。
- `features/tt_posts/service.py`：CPU 白名单配置、人工运行与队列门禁判定、强制私密策略、GPU 请求标记。
- `features/tt_gpu/worker.py`：GPU 白名单配置、请求契约、目标/来源校验和幂等发布。
- `static/tt-post-pool.html`：按钮与提示。
- `scripts/test_tt_posts_*.py`、`scripts/test_tt_gpu_worker.py`：测试。

### 配置

CPU 与 GPU 均使用独立但一致的环境变量：

- `TT_POST_MANUAL_CANARY_ENABLED`
- `TT_POST_MANUAL_CANARY_ACKNOWLEDGEMENT`
- `TT_POST_MANUAL_CANARY_ID`
- `TT_POST_MANUAL_CANARY_EXPIRES_AT_UTC`
- `TT_POST_MANUAL_CANARY_ACCOUNT_ID`
- `TT_POST_MANUAL_CANARY_MATERIAL_ID`
- `TT_POST_MANUAL_CANARY_CONTENT_ID`
- `TT_POST_MANUAL_CANARY_GPU_JOB_ID`
- `TT_POST_MANUAL_CANARY_OUTPUT_SHA256`
- `TT_POST_MANUAL_CANARY_OUTPUT_SIZE`
- `TT_POST_MANUAL_CANARY_PROFILE`

CPU 额外使用：

- `TT_POST_MANUAL_CANARY_POOL_ID`

GPU 额外使用：

- `TT_POST_MANUAL_CANARY_ORIGIN`

正式门禁变量继续保持 `0`。

### 数据结构

不新增数据库字段。队列通过 `tt_post_schedule_run.queue_id` 反查，必须是人工运行并与配置目标完全一致。

### API

- `GET /api/admin/tt-posts/schedule`：新增非敏感的 `manual_canary`、`manual_canary_ready` 与 `can_publish_now`。
- `POST /internal/tt-posts/run-now`：请求结构不变；服务端根据白名单判断。
- CPU 到 GPU 使用独立的 `POST /internal/tt-post/canary-publish`；普通 `/internal/tt-post/publish` 不接受一次性测试。
- canary 凭据封装使用独立 operation `canary_publish`，不能在普通发布接口重放。

### 异常边界

- 白名单配置不完整：服务启动失败并明确指出配置无效。
- 目标不匹配：发布前失败，不调用 TikTok。
- 来源不匹配：GPU 失败，不调用 TikTok。
- TikTok 明确拒绝：队列失败、素材消耗、错误原样留存。
- 请求结果未知：队列进入 unknown，仅允许人工核对，禁止重试。

## 验收标准

- 指定账号、素材、Job、Drama ID 的人工按钮可用。
- 点击前生产队列、`publish_id` 仍为 0。
- 每日排期保持 0/禁用；自动触发无法绕过门禁。
- 实际请求为 `SELF_ONLY` 且所有互动/商业开关关闭。
- 其他目标和普通直发在三重门禁关闭时仍被阻断。
- 同一 Job 至多调用一次 TikTok 初始化接口。
- TikTok 返回的真实错误码、log ID 或 publish ID 可在任务记录中查看。

## 风险与待确认

- TikTok 可能因客户端未审核、URL Property 未验证或内容策略直接拒绝，这是本次测试的预期可观测结果，不代表系统故障。
- 若 TikTok 返回 `publish_id`，后续只允许状态核对，不再次初始化。
- 正式开放仍必须完成平台审核、URL Property 验证和素材合规整改，本次白名单不能替代正式门禁。

## 变更记录

- 2026-07-31：创建一次性人工私密发布测试方案。
