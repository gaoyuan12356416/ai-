# SA 评审意见

## 结论

有条件通过。安全边界、耐久主键、旧/新调度互斥和 unknown fence 已落地；跨产品素材留作 V2。

2026-08-18 V2 复审：原候选的窗口 MySQL 双扫和到点重活已删除。指标改为 FB 独立 READY 日缓存；调度改为 future due-slot + plan + prepare + 到时 Graph；产品范围明确冻结为 Dramawave。GPU worker 制品和 Graph 已有对象状态只读核验仍是开 live gate 前的外部集成门禁，不影响 gate=0 候选验收。

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

## 决策记录

- Page ID 为发布和去重耐久主键；组 ID 仅表示来源。
- Graph 对象处理失败为 `failed_without_retry`，同一任务永不重发。
- live gate 默认关闭，不以真实发帖验收。

## PM 修订确认

已同步到需求、代码、测试、API 和部署文档。
