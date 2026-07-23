# 012.x-post-material-pool SA 评审意见

## 结论

通过。人工全局素材池、FIFO、三条成组、永久排重和“仅确认成功才发布”的状态模型成立；Dramawave 产品门禁、双向跨表占用、派生统计和 1000 条安全扫描窗口均已在代码与离线回归中闭环。生产部署仍受副本迁移和 live composite 门禁约束。

## 问题清单

| 编号 | 严重级别 | 位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| SA-001 | P1 | pool/queue 跨表占用 | 仅防“queue 先存在再入池”不足以覆盖“先入池再走 legacy/canary queue” | service 校验与 SQLite 触发器同时按 `pool_item_id`、`material_key` 双向防护；查询和删除同口径 | 已关闭并回归 |
| SA-002 | P1 | manual selector | 目标始终是 Dramawave W2A，按 ID 直选仍必须精确校验素材产品 | 查询 `ads_custom_source.product` 并要求精确等于 `Dramawave`；增加其他产品负例 | 已关闭并回归 |
| SA-003 | P1 | 状态转换 | known failure/unknown 若改成 published 或释放，会造成重复 Post | 主状态只保留 unpublished/published；queue 派生失败/待核查且永久占用 | 已采纳 |
| SA-004 | P1 | 批次原子性 | 不足三条时若先创建部分 queue，会导致当天账号不齐 | 全部 selector/媒体预检在计划前完成；一次事务创建 run+3 queue | 已采纳 |
| SA-005 | P1 | FIFO 并发 | runner 快照与计划提交间池状态可能变化 | 计划事务重新校验池状态、material key、created_at、占用和顺序 | 已采纳 |
| SA-006 | P2 | pool summary | `validation_failed` 若仍进入 summary.available，会与筛选/逐项状态矛盾 | summary 使用与 availability 相同口径并补断言 | 已关闭并回归 |
| SA-007 | P2 | 扫描窗口 | 只取合规候选上限 50 不能同时作为原始池扫描上限 | 原始池按 scan limit 读取最老 1000 条，再按顺序保留最多 50 条合规候选 | 已关闭并回归 |
| SA-008 | P3 | legacy 配置 | `material_keys_path` 和旧 spend selector 仍保留但正式路径不再使用 | 后续清理或明确 legacy 兼容，不阻塞本需求 | 非阻塞技术债 |
| SA-009 | P2 | 检查回写 | 最多 1000 个拒绝若一次提交，会超过 Sidecar 单次 100 条限制并被 best effort 吞掉 | runner 按 100 条切分，增加 205 条 100/100/5 回归 | 已关闭并回归 |

## 决策记录

- 素材池是全局单池，不按三个 X 账号拆分。
- 规范 `material_key` 是跨池、queue、日期和账号的全局排重键。
- “被 queue 占用”即永久去重；known failure 和 unknown 都不自动补发。
- 池主状态不承载执行态，后台用 queue/log 派生 `available`、`validation_failed`、`occupied`、`failed`、`needs_review`、`published`。
- `source_date` 只保留批次日历与日志兼容，不再参与素材排名。
- 计划创建前必须凑齐三条并完成媒体预检；否则三个账号全部不发。

## PM 修订确认

需求文档已写入产品门禁、双向占用、三条成组、状态派生、1000/50 两级窗口和 100 条检查回写批次。全部 P1/P2 已在最终工作树复核，完整离线回归 139/139 通过。
