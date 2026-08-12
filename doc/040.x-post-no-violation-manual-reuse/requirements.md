# 040 X 发布取消违规历史校验与手动素材复用

## 生效规则

1. 所有 X 发布路径不再查询 `ads_facebook_violations`、`ads_tiktok_violations`、`ads_twitter_violations` 或 `ads_resource_audit`，也不因这些表中的历史记录拒绝素材。
2. 队列中的四个既有违规计数字段继续保留，新的 X 候选固定写入 `0`，保持 schema、DTO 和历史日志兼容。
3. 素材来源/资源危险标签、Dramawave 产品与映射、可投放时间、媒体格式/大小/时长、账号授权、token 会员资格、幂等和未知结果保护不变。
4. `trigger_source='manual'` 的运营手动批次允许选择已在素材池、当前队列或历史队列中的素材；不得删除、解绑或改写旧池行、旧队列或旧日志。
5. 同一个手动请求中的素材 ID 和账号 ID 仍必须各自唯一，数量必须相同。每次重新手动发布都创建新的 `x_post_manual_run` 和新队列，且 `pool_item_id=NULL`。
6. 自动素材池、daily、schedule、catch-up、canary 和 `auto_template` 继续拒绝任何已有队列历史的素材，不获得手动复用例外。
7. 单条队列一旦进入 `post_creating` 或 `unknown_outcome=1`，仍禁止自动重试该队列；手动素材复用不等于重放旧队列。

## 存储边界

- 移除 `x_post_queue(material_key)` 的全局唯一索引，保留非唯一查询索引。
- `BEFORE INSERT/UPDATE` 触发器仅对 `x_post_manual_run.trigger_source='manual'` 放行重复 `material_key`；其他路径若发现任意历史队列立即中止。
- 素材池绑定必需触发器仅对运营手动队列放行 `pool_item_id=NULL`；`auto_template` 仍执行原规则。
- 迁移前若存在两个非运营手动队列复用同一 `material_key`，必须 fail closed；合法的运营手动重复历史可重复迁移且不删除数据。

## 验收边界

- 本地和生产备份副本必须验证迁移可重复、SQLite `integrity_check=ok`、触发器存在且自动路径仍去重。
- 部署验收不得创建手动任务或真实 X Post；只允许健康检查、schema/账本核对和自然 timer 的 `no_pending/no_due` 证据。
