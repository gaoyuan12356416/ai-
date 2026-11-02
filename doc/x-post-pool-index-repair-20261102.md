# X Post素材池读取与添加超时：在线索引修复

## 已确认的原因

素材池按 `q.pool_item_id=p.id OR q.material_key=p.material_key` 关联发布队列。旧 material_key 索引带 `WHERE material_key<>''`，查询未显式包含该条件，SQLite无法采用这个部分索引，转而逐素材扫描整个队列表。生产冻结副本有4347条素材、5379条队列，原查询23.134秒，长读事务还会阻塞DELETE journal模式下的写提交和每请求的存储初始化，形成锁等待与502/503。

## 最终修复

新增完整非唯一索引 `idx_x_post_queue_material_lookup ON x_post_queue(material_key)`，保留原部分索引、关联SQL、计数、排序、筛选、唯一约束及全部发布保护。完整索引可被正在运行的原代码立即使用，不必重启，也不改变运行中上传任务。`ensure_storage` 包含幂等建索引语句，保证新库和后续迁移同样具备该索引。

先在独立SQLite在线备份验证，再推送GitHub、从GitHub取回精确提交并备份线上代码及数据库，用有界事务建立索引。仅部署service.py，不覆盖其他近期功能文件。上线验证真实列表、筛选、翻页和已有素材重复添加；测试不得发布帖子或增加待发布素材。

## 验证

- 同份冻结数据，原代码配合完整索引的查询0.029秒；列表首屏全部字段、分页与汇总逐项相同。未通过空数组/缓存旧结果掩盖故障。
- 新测试覆盖真实查询计划、已打开连接在线采用索引、迁移幂等、并发读取和重复添加、无新发布队列。
- 现有X Post专项测试继续通过。
- 扩展OAuth/UI历史测试有与索引无关的既有失败，需与未修改基线比较，不宣称扩展全套无失败。

## 回滚

回滚代码只恢复备份的两个service.py，不覆盖任何SQLite业务记录。索引是非唯一的可逆性能结构，可保留；确需删除时，在有界数据库事务内执行 `DROP INDEX IF EXISTS idx_x_post_queue_material_lookup`，会恢复旧查询性能，不建议在繁忙期执行。不得恢复旧数据库覆盖已发生的素材入池、发布记录或Token更新。无需为本索引改变定时器、发布开关、账号状态或重启上传进程。


## X Post素材池读取性能（2026-11-02）

- 页面由当前应用代码验证为 `/x-post-material-pool.html`，列表及添加接口为 `/api/admin/x-posts/material-pool`，主API经loopback X侧车读取同一发布账本。
- 素材池关联发布队列使用 `pool_item_id OR material_key`。旧索引 `idx_x_post_queue_material_key` 带 `WHERE material_key<>''`，原关联条件不能使用此部分索引，必须同时保留完整的非唯一索引 `idx_x_post_queue_material_lookup ON x_post_queue(material_key)`。不要在清理“重复索引”时删掉该完整索引。
- 修复提交 `a28d8bbaee71681af50a289f15bc007081381582`，GitHub分支 `codex/x-pool-query-lockfix-20261102`。`ensure_storage`已幂等创建完整索引；冻结副本查询23.134秒降至0.029秒，首屏字段、分页和汇总完全一致。
- 完整索引可在线建立，现有连接会重新规划查询；不要为此强制重启正在上传/创建帖子的X侧车。线上建索引事务必须有界，先备份，再检查索引列、非唯一及非部分属性，并验证零业务DML。
- 不修改素材、队列、账号/Token、发布状态、去重保护或定时器。修复/回滚不恢复旧SQLite覆盖新业务事实。
- 生产副本包含当前运行叠加功能，必须以当前release为底只替换核实过的service.py；其他文件不得随旧基线整包覆盖。部署文件、备份及回滚细节记录在本次COS报告和仓库 `doc/x-post-pool-index-repair-20261102.md`。
