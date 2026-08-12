# 040 测试用例

| 编号 | 场景 | 预期结果 |
| --- | --- | --- |
| TC-040-01 | daily selector 处理原本有违规历史的素材 | 不执行四张违规历史表 SQL；候选可继续；四个计数字段为 0 |
| TC-040-02 | 素材池/手动 selector 处理原本有违规历史的素材 | 不执行四张违规历史表 SQL；其余标签、映射、媒体门禁正常 |
| TC-040-03 | 手动发布已存在历史 queue 的素材 | 创建新的 manual run/queue；旧 queue/log 不变 |
| TC-040-04 | 手动发布已在素材池的素材 | 创建 queue 且 `pool_item_id=NULL`；池行不修改 |
| TC-040-05 | 同一手动请求重复素材 | 400，无 run/queue |
| TC-040-06 | auto-template 复用历史素材 | 409，无新 queue |
| TC-040-07 | daily/schedule/catch-up/canary 复用历史素材 | 原有去重错误，无新 queue |
| TC-040-08 | schema 连续迁移两次 | 合法手动重复保留；索引/触发器稳定；完整性 ok |
| TC-040-09 | 非手动 INSERT/UPDATE 绕过应用层复用素材 | SQLite 去重触发器中止 |
| TC-040-10 | 生产无发帖验收 | queue/log/Post/unknown/active 数量不增加；服务和 timer 正常 |
