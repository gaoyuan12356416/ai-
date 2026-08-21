# SA 评审意见

## 结论

有条件通过。安全边界、耐久主键、旧/新调度互斥和 unknown fence 已落地；跨产品素材留作 V2。

2026-08-18 V2 复审：原候选的窗口 MySQL 双扫和到点重活已删除。指标改为 FB 独立 READY 日缓存；调度改为 future due-slot + plan + prepare + 到时 Graph；产品范围明确冻结为 Dramawave。GPU worker 制品和 Graph 已有对象状态只读核验仍是开 live gate 前的外部集成门禁，不影响 gate=0 候选验收。

2026-08-20 宏扩展复审：有条件通过。`{{desc}}` 必须从 `ads_drama_resource.desc` 按 app/content/language 批量、确定性读取；`{{url}}` 必须冻结为独立 FB 命名空间短链，W2A base 固定 `/ads/0/2049/view`、`af_channel=AIpost`，并在任何 Graph POST 前完成不可变 wrapper 写入。生产只做 closed-gate 部署和只读/404 验证，不以真实模板或发帖验收。

2026-08-21 日预制复审：原4小时滚动窗口不足以覆盖当前40个/日、单任务约20分钟的串行GPU基线。跨过北京午夜持续启用的模板直接枚举今天剩余和明天完整时隙；当天首次启用/重启用/新版本仅生成明日完整时隙，且不删除既有同版本自动任务。次日随机计划、Page、素材与成片提前冻结，发布领取继续受计划时间约束。模板停用/版本漂移必须在prepare和Graph两层fail closed。

切换安全性要求预制与Graph分门禁：先 `prebuild=1/live=0` 生成真实 ready 成片并验证 `prepared_at_utc`、SHA/profile与计划时间，再打开Graph；不能为了测试预制而同时放开真实发布。

## 问题清单

| 编号 | 严重级别 | 位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| SA-01 | P0 | Page SQL | 误用 owner_user_id 且仅 type=0 | 改为 g.user_id，纳入0/1 | 已关闭 |
| SA-02 | P0 | 黑名单 | schema 错误 | 默认 ads_setting | 已关闭 |
| SA-03 | P0 | 素材冻结 | 每Page重扫指标 | 每运行一次候选快照 | 已关闭 |
| SA-04 | P0 | Graph | ID误作最终发布 | submitted + reconcile | 已关闭 |
| SA-05 | P0 | 调度 | 旧/新队列可能双发 | 启用与运行前冲突检查 | 已关闭 |
| SA-06 | P1 | 重复Page | 组独占不足 | Page联集+唯一索引 | 已关闭 |
| SA-07 | P1 | 产品 | 历史支持跨产品 | V1同产品限制并明示 | 已关闭 |
| SA-08 | P1 | 吞吐 | 单任务执行积压 | Graph每轮4并发/4任务；GPU每轮串行1任务；容量/积压闭锁 | 已关闭 |
| SA-09 | P0 | URL冻结 | 若运行时临时拼长链，重试可能漂移 | task ID 后同事务冻结 short/long/message | 已关闭 |
| SA-10 | P0 | Graph边界 | wrapper失败后仍可能发帖 | 发布前物化，失败即停止且Graph调用数为0 | 已关闭 |
| SA-11 | P1 | 描述查询 | 逐Page查询导致N+1或语言错配 | 仅宏启用时按每页content集合聚合读取并过滤歧义 | 已关闭；生产只读EXPLAIN通过 |
| SA-12 | P0 | 预制吞吐 | 4小时窗口无法覆盖40个串行长视频任务 | 直接冻结今天剩余+次日完整时隙，提供24–48小时预制余量 | 已关闭 |
| SA-13 | P0 | 版本门禁 | 已ready旧版本任务可能在编辑后继续发布 | prepare/execute claim校验enabled+current version，旧版本安全跳过 | 已关闭 |
| SA-14 | P0 | 切换门禁 | 单一live开关无法在不放Graph时验收真实预制 | 增加独立prebuild gate，health同时展示两者 | 已关闭 |
| SA-15 | P0 | 迟到策略 | GPU失败或停用后重启可能无限晚发 | 自动due/task超过10分钟宽限落missed/skipped，manual不受影响 | 已关闭 |
| SA-16 | P0 | Graph状态竞态 | ready领取后停用/升版可能让旧running继续提交 | claim为不可逆边界；running存在时同事务拒绝停用/编辑 | 已关闭 |
| SA-17 | P0 | manual恢复 | 停用模板可排manual并在日后重启用后意外发布 | disabled拒绝run-now；停用永久取消未提交manual；列表按钮置灰 | 已关闭 |
| SA-18 | P1 | planner租约ABA | 旧plan worker晚回调可能覆盖新worker租约 | due ID + owner + expires组成令牌，所有建单/完成/延后均核对 | 已关闭 |
| SA-19 | P1 | legacy ready | prepared_at为空的旧ready不可领却会占backlog | 迁移与运行期均fail closed终结并刷新run | 已关闭 |

## 决策记录

- Page ID 为发布和去重耐久主键；组 ID 仅表示来源。
- Graph 对象处理失败为 `failed_without_retry`，同一任务永不重发。
- live gate 默认关闭，不以真实发帖验收。

## PM 修订确认

已同步到需求、代码、测试、API 和部署文档。
