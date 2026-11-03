# 接口兼容性与运维入口

本次不新增公开 API，不改变原发布请求、回执或鉴权格式。既有 X broker 的 begin-operation/checkpoint/fail 继续执行同样的账号、租约、generation 与平台 intent 检查。Post/Repost 不增加自动重试。

内部归因读取 `/internal/x-posts/attribution/resolve` 的请求与响应不变。调用方对只读的 HTTP 5xx/网络超时、连接拒绝或重置增加最多三次、有总时间上限的重试；校验失败和明确 4xx 直接失败。短链创建与冻结只发生在有效归因返回以后，不纳入重试循环。

维护 CLI 均默认通过独立 plan/evidence/apply 阶段执行。具体参数和错误拒绝条件见以下同目录文件：

- prepost-recovery.md：未到 Post 的已知失败、原媒体、精确下一代授权与节点 outbox 证据。
- attribution-recovery.md：精确归因零写入失败，只恢复中央状态。
- manual-recovery.md、schedule-zero-write-recovery.md：已冻结的旧任务原身份恢复，不生成替代任务。
- drama-episode-recovery.md：1029/1031 第二次精确预检恢复；要求部署文件与 Git 提交相符。
- cpu-node-activation.md：候选验证、自然排空和实际新进程验收。

维护拒绝码用于表示计划漂移、未知/成功历史、活动租约、媒体改变、证据过期、部署哈希不匹配等真实前提未满足。不能通过改计划文件、删除旧回执或清零 attempt 绕过。恢复审计只追加，保留操作前完整状态、计划哈希及备份路径。
