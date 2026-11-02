# 制作租约与跨节点恢复

FB/TT 的首次原生制作描述器保存任务、素材、账号、输入 URL、裁尾、模板、编码 profile，以及适用的素材目录指纹。调度器把描述器要求的 backend/profile/catalog 能力冻结到任务；后续领取只匹配这些能力。新一代原生领取复用完整描述器，不重新选材或选择另一套制作参数。

协调器的制作租约过期后，新一代仅能接管同一 source/stage 已持久化的上一代原生制作绑定。FB 校验原 lease owner/token，TT 校验原 claim token/worker，并再次检查素材身份及不存在发布证据。更新限定单条任务，不清理同账号其他任务。原发布状态、未知结果和已开始的发布操作均不能按此路径重试。

已确认的原生成片由协调器读回并推进到发布阶段；工作节点已经上传、但回执未落原生账本的成片由已验证的 COS 恢复机制处理。没有持久化证据时不把本地临时产物当成已完成。

## 仍需人工核对的窄窗口

原生 store 的领取提交与 `pipeline_fb_claim` / `pipeline_tt_claim` 绑定插入是两个事务。若进程恰好在两者之间退出，原生账本可能保留一个没有对应桥接绑定的制作租约。此时不会仅凭 `pipeline:` 前缀抢租约，也不会重新制作或发布；后续领取以 `canonical_task_not_claimable` 或 `canonical_prepare_reclaim_conflict` 明确失败并保留原任务身份，需要核对原生账本、节点与已上传成片后处理。该窗口不属于已实现的自动恢复。

`scripts/test_pipeline_prepare_recovery.py` 使用真实 SQLite 原生账本覆盖正常跨代接管、旧回执 fencing、错误 token/素材拒绝、以及上述两个事务间中断的隔离行为。

## YouTube 付费生成与审核版本

YouTube 同一版本的自动跨代领取仅允许找回已有、通过 COS 身份核验的成片；描述器顶层标记 `recovery_only`，冻结的生成请求不变。已有生成意图但缺少合格成片时，以 `youtube_generation_interrupted` 失败并等待人工核对或明确重试，不因租约过期再次付费生成。

审核驳回产生新的 `current_version`；用户对 `generation_failed` 明确重试会记录新的 `generation_retry_id`。两者构成 `source_revision=version:retry_id`，允许同一来源的制作任务重新入队，同时保留递增 generation 和旧回执历史。暂停积压、发布中、未知结果和仍活跃的制作均不会因此自动解锁。每个新生成版本完成后仍进入原有封面审核流程。
