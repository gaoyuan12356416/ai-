# CLI 与内部接口

无新增公开 Web API。入口：server-monitors/meta-violation-material-cleanup/cover.py。

## CLI

```bash
python cover.py --config /mnt/data-disk/meta-violation-material-cleanup/config.json preview --max-ads 256
python cover.py --config /mnt/data-disk/meta-violation-material-cleanup/config.json scan
python cover.py --config /mnt/data-disk/meta-violation-material-cleanup/config.json work --once --max-ads 256
python cover.py --config /mnt/data-disk/meta-violation-material-cleanup/config.json status
python cover.py --config /mnt/data-disk/meta-violation-material-cleanup/config.json notify
```

preview 使用隔离账本并只读 Graph；scan 读取源库和入队；work 需要 cover_live_enabled=true 和进程锁，不加 --once 持续消费。status 不恢复/重放任务；notify 不扫描/修改广告。

## Graph

| 操作 | 接口 | 限制 |
| --- | --- | --- |
| 读对象 | GET /{id} | 验证 ID/账户/完整字段 |
| 创建封面 | POST /act_{account}/adcreatives | 完整 payload，picture URL |
| 绑定原 Ad | POST /{ad_id} | 仅 creative={creative_id:new_id} |
| 查引用 | GET /act_{account}/ads | 所有非删除状态含归档，完整分页 |
| 删旧 Creative | DELETE /{creative_id} | 无引用、共享锁和一次意图 |

写请求在取得配额后执行 before_send 持久屏障。明确失败不重试，超时/中断/不确定响应只读核实。

## 队列 API

- enqueue(batch,candidate)：唯一键 account/ad/oldCreative；ready_tasks 只读 sealed 批次。
- mark_task_write(id,stage='binding',intent)：持久保存新 ID 和预期 Ad。
- finish_task(id,'succeeded',result)：要求 confirmed_binding 与不同新 ID，同事务排旧 Creative 清理。
- claim_resource(key,account,batch,payload)：单次创建；未知或失败不重建。
- is_cover_creative(account,creative)：索引查询已接受/已绑定封面，避免源库同步后每小时重复转换。
- resolve_resource(key,result)：仅未知已保存 ID、verified=True、readback 同 ID/账户才接受；只重排未发送绑定且仅被此资源阻塞的任务。
- claim_delete_once(account,creative)：提交本地一次意图。finish_delete 不允许失败重试。
- recover_inflight()：只有持有独占进程锁并证明旧 worker 退出的调用方可执行；构造函数不恢复。
- notification_candidates(include_pending=False)：返回 counts 和下次 notification_attempt。True 用于完成首批但仍有积压；mark_batch_notified 保存实际已发送快照。

## 共享 Store

- reserve_cover_resources(owner,account,ad_id=None,creative_ids=()) 原子预留。
- release_cover_resources(owner,unknown=False,verified=False) 对未知必须经过只读证明再 verified 释放。
- claim_cover_delete_once(owner,account,creative) 提交全局本流程一次意图。
- get_cover_delete_once(account,creative) 只读返回 owner/status/result，无恢复副作用。
- finish_cover_delete(owner,account,creative,status,result) 成功需 confirmed_deleted=True 并写历史成功回执，未知继续围栏。

缺失对象、权限失败或读取异常不等于删除成功。元数据拒绝 token/password 等凭证字段。
