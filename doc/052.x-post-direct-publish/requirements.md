# 052.x-post-direct-publish 需求与技术设计

## 背景

2026-08-20 15:11 的素材池定时任务按时认领，但旧实现必须先串行下载、探测、必要时修复足够覆盖全部目标账号的媒体，之后才创建任何队列。运行近 50 分钟、写入超过 2 GiB 临时数据后仍为 `claimed`、0 queue、0 log、0 X attempt。媒体在正式发布阶段还会再次下载和探测，因此建队前媒体预检形成重复 I/O 和头阻塞。

用户明确要求取消定时素材池、短剧池的建队前媒体预检：池内有多少可用元数据候选就先建多少队列，真正发布该条时再完成唯一一次必要的媒体下载/探测并立即调用 X。

## 目标

- 素材池和短剧池定时任务不再因建队前媒体下载、ffprobe 或 GPU 修复等待几十分钟。
- 先按源数据、账号、语言、合规、FIFO/归属和全局去重冻结非空有序子集，再逐条执行媒体下载、探测和 X 发布。
- 一条确定性失败不阻断同批后续正常素材或短剧；只有 X 限流或结果未知才停止。
- 保持 Premium 长视频与同语言 Relay、幂等、未知结果禁止盲重试等安全边界。

## 范围

### 包含

- 自动排期 `source_type=material|drama` 的候选规划、建队、发布和恢复。
- `x_post_queue` 增加显式延迟媒体校验模式。
- 素材源库时长作为轻量路由提示；短剧未知时长对非 Premium 目标预先冻结同语言 Premium Relay。
- 已知短剧失败的局部隔离与后续队列继续执行。
- 生产 15:11 已认领、零写入批次用同一 `schedule_run` 续跑。

### 不包含

- 手动发布、X Auto、legacy daily、catchup 的既有完整媒体预检合同。
- 删除实际发布阶段的下载、类型/尺寸/编码/时长/大小探测。
- 对未知结果、429、账号身份、Token、语言、违规标签、映射、FIFO、去重或存储门禁的放宽。
- 为验证额外创建不属于真实排期的 X Post。

## 用户故事 / 业务规则

1. 作为运营，我希望定时点到达后立即把正常元数据候选建成队列，而不是等待全池媒体预检。
2. 素材候选仍须来自当前 FIFO 快照；视频源时长 `>140s` 且目标非 Premium 时必须冻结同语言 Premium Relay，无 Relay 时只跳过该候选。
3. 短剧源时长未在源表冻结：Premium 目标 direct；非 Premium 目标预先冻结同语言 Premium Relay，避免实际为长视频时确定性失败。
4. 延迟模式队列在任何 X 写入前只下载/探测一次，并用真实时长生成 `af_channel` 和选择 X upload category。
5. 普通已知失败持久化为 failed 后继续下一队列；短剧失败剧保留原归属并局部记录错误，不污染全池 `needs_review`。
6. 429 或 `unknown_outcome` 立即停批；未知短剧仍进入全局 `needs_review`。
7. 已存在完整预检队列继续严格核对 SHA-256、字节数、时长和 140 秒边界。

## 交互与流程

```text
到点认领 -> 账号/存储/源数据/语言/合规/FIFO检查
         -> 冻结非空有序子集 queue(media_validation_mode=deferred)
         -> 对 queue 逐条：实时账号校验 -> 下载/probe -> 短链/归因
         -> upload + Create Post ->（如 Relay）目标 Repost -> 账本终态
         -> known failure 继续；429/unknown 停止
```

## 技术设计

### 影响模块

- `features/x_posts/selector.py`：返回素材源时长路由提示。
- `scripts/x_post_schedule_runner.py`：用轻量计划替代媒体预检，保持部分容量与顺序发布。
- `features/x_posts/service.py`：显式延迟模式、存储迁移、发布时真实探测、短剧失败隔离与 run 聚合。
- `features/x_accounts/oauth_service.py`：延迟队列实时完整账号校验、串行单文件存储门禁。

### 数据结构

`x_post_queue.media_validation_mode TEXT NOT NULL DEFAULT 'preflight'`：

- `preflight`：旧合同，正式批次必须具备并严格核对指纹。
- `deferred`：仅定时素材/短剧计划允许；建队时 SHA 为空、size 为 0，实际发布以下载结果为准。

加列为向后兼容迁移；历史行全部默认为 `preflight`。

### API / 接口

- 内部 schedule-plan candidate 新增 `media_validation_mode=deferred`。
- 字段仅由 scheduler bearer 路径接受，普通 enqueue/manual/daily/catchup 不得伪造。
- 对外管理员 API 无破坏性字段变化。

### 异常与边界

- 延迟 direct 实际探测为长视频、实时账号非 Premium：确定性失败并继续后续队列。
- 延迟 Relay 的路由时长只是冻结 Relay 的提示，真实时长只用于最终归因和上传类别，不做预检漂移对比。
- 实际下载/probe/上传前失败有 durable log/queue failed，不自动重试该 idempotency key。
- Create Post/Repost 结果未知保持 no-retry fence。
- 绑定短剧的普通已知失败只隔离该剧和其绑定账号；其他账号的正常剧继续。未知失败仍全局暂停。

## 验收标准

1. 注入“调用即失败”的 scheduler downloader/prober/repair client，素材和短剧仍能创建 deferred queues。
2. 计划创建前不会写媒体临时文件或调用 ffprobe/GPU repair。
3. deferred queue 正式发布只下载和探测一次，并按真实时长生成链接/上传类别。
4. preflight queue 缺指纹或内容漂移仍被拒绝。
5. 素材长视频 direct/relay、短剧未知时长 relay 均保持同语言和 Premium 门禁。
6. 第一条素材或短剧普通已知失败后，第二条仍执行并可成功；最终批次为 `completed_with_errors`。
7. 429、Post/Repost unknown 仍停止后续队列。
8. 生产部署前 15:11 run 保持 claimed、0 queue/log/attempt/unknown；部署后续跑同一 run，不创建重复批次。
9. X 专项全量自动化回归通过；SQLite quick/integrity check、FK、队列/日志不变量通过。

## 风险与待确认

- 取消建队前 GPU repair 后，可修复但原始格式不合规的媒体会在实际发布阶段成为已知失败；这是用户明确选择的效率优先边界。后续若恢复修复能力，应改为异步预热，不得重新阻塞到点建队。
- 短剧源数据没有可靠时长，非 Premium 目标会保守使用 Relay；短视频也可能产生一次源 Post 加一次目标 Repost。
- 不做额外真实 Post canary；以真实已认领批次作为授权范围内的自然验收。

## 变更记录

- 2026-08-20：首次冻结需求；用户明确以发布时校验替代建队前媒体预检。
