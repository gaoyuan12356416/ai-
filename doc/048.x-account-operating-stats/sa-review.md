# SA 评审意见

## 结论

通过。采用独立只读统计模块和数据盘缓存，不扩展 OAuth Sidecar DTO，不让页面请求扫描外部库。

| 编号 | 级别 | 问题 | 决策 | 状态 |
| --- | --- | --- | --- | --- |
| SA-001 | P0 | relay log 不能直接代表原 Post actor | direct 按 q.account_id；relay 按确认 source Post 的 relay_account_id | 已关闭 |
| SA-002 | P0 | campaign 缺失/冲突不能均摊 | 只接受唯一精确 c，其余未归属 | 已关闭 |
| SA-003 | P0 | 直接 driver 绕过宿主 gate | 仅 `/usr/bin/mysql`，拒绝 mysql.real | 已关闭 |
| SA-004 | P1 | 页面扫描 MySQL | twice-daily 原子缓存，API 只读 JSON | 已关闭 |
| SA-005 | P0 | 现网 OAuth runtime 与分支不同 | 从 5d5965d 主 API composite 开发，oauth_service 禁改 | 已关闭 |
| SA-006 | P1 | ledger relay 字段不能覆盖 queue 冻结 actor | JOIN queue 并逐项校验，冲突不归属 | 已关闭 |
| SA-007 | P1 | 收入真实列为 `campaign`，且生产需索引约束 | 改用 campaign 并 `FORCE INDEX(idx_site_event_time)` | 已关闭 |
| SA-008 | P1 | 仅校验 mysql.real 名称不足以阻止其他漂移目标 | `/usr/bin/mysql` 必须精确 resolve 到批准 wrapper | 已关闭 |
| SA-009 | P2 | 仅 TTL 无法识别跨北京日/未来时钟 cache | 加业务日与 5 分钟未来偏差门禁，UI 显示口径日期 | 已关闭 |
| SA-010 | P1 | failed/reserved log 不应产生收入归属 | 仅确认 published + post ID 建 campaign map | 已关闭 |

以上决策已同步需求、代码、测试和部署文档。
