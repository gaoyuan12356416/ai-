# SA 评审意见

## 结论

有条件通过。采用“additive intake 表 + 独立 prepare runner + 原子转入既有 ready pool”，可以把长耗时工作移出用户请求，又不破坏现有发布池的强约束。以下问题须在代码与测试中闭环。

## 问题清单

| 编号 | 严重级别 | 位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| SA-001 | P0 | 数据模型 | 若直接让既有 recurring pool 接收空成片，会使未制作素材被发布逻辑误消费。 | 新增 intake 表；既有 pool 只允许完整 ready 成片。 | 已采纳 |
| SA-002 | P0 | 完成事务 | 先写 ready pool 后更新 intake 可能在崩溃时形成分裂状态。 | 两步放在同一个 `BEGIN IMMEDIATE` 事务，成功或回滚。 | 已采纳 |
| SA-003 | P0 | 并发 | 长 GPU 调用超过租约时，旧 worker 可能覆盖新 worker。 | claim token + lease + 续租 + 完成时 fencing 校验；token 不向公共 API 暴露。 | 已采纳 |
| SA-004 | P1 | 排序 | 简单全局 FIFO 不能表达账号内顺序；简单按账号并发又可能越过前项。 | 只允许每个账号最老的活动项成为候选，再从候选中取全局最早项。 | 已采纳 |
| SA-005 | P1 | 可用性 | 只靠 path kick 可能因触发丢失造成永久等待。 | path 快速唤醒，timer 每分钟持久化兜底。 | 已采纳 |
| SA-006 | P1 | 发布隔离 | 复用 `tt-post-runner.service` 会让长制作延误发布和 reconcile。 | 独立 prepare service、lock、path、timer。 | 已采纳 |
| SA-007 | P1 | 超时单位 | GPU 超时、内部请求与 systemd 超时单位混淆会导致中途杀进程。 | 文档统一为秒并验证 `process >= gpu + 60`、unit timeout 更大。 | 已采纳 |
| SA-008 | P2 | 兼容 | 旧前端仍调用 `/materials/prepare`。 | 保留别名，但语义改为快速校验并返回明确状态。 | 已采纳 |

## 决策记录

- 决定不重建 `tt_post_recurring_pool`，避免迁移时放宽 `NOT NULL/CHECK` 和污染 ready 语义。
- 决定 intake 保存完整冻结请求摘要；同键同摘要重放返回已有记录，同键不同摘要失败。
- 决定预制作失败最多自动尝试 5 次；终态业务错误不重试。
- 决定完成制作前读取账号最新发布设置和 Creator Info，避免使用校验阶段的陈旧时长/权限。
- 决定上线验证不打开 live/direct-audit/url-property gates，不调用真实 TikTok 发布接口。

## PM 修订确认

需求已补充以下验收点：校验不触发 GPU、queued 即算入池但不可发布、页面状态可恢复、严格账号 FIFO、租约恢复、独立 runner、部署回滚与禁止真实发布验证。
