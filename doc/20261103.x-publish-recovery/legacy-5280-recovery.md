# 原队列 5280 的一次性恢复

本工具只接受 queue 5280 / log 5229 / account 49 (@Vani4ka94) / manual run 1325 / dispatch job 7818。用户已明确账号解锁；应用时仍须确认当前账号 active、publish_approved，并显式记录解锁确认和操作者。5367 属于未获解锁确认的账号 123，5333 是媒体 status 非 JSON，均不在范围内。

原生日志在 2026-11-01T19:17:39Z 记录明确的媒体 initialize HTTP 403、attempt_count=1、unknown_outcome=0，无媒体 ID、Post ID 或发布时间。原错误全文 SHA256 固定为 `791af13c90e5f4081f86601c1a5733a9da655d2bae87bdf647d2974c03c36f07`。原生 claim/operation/checkpoint、中央所有代次操作/回执/领取记录、转发和隔离记录均须为空；两个节点必须提供 120 秒内的完整空回执证据。即使发现已 ACK 的 known-failed 回执也拒绝，因为它将改变该条“原生旧日志、尚无调度执行”的证据类型。

成片 SHA256 为 `4f5a2046536eaa4b158af9e114ab109d77ece30a2f0ca9e52c3e40e8147e036b`，135115095 字节，220.002 秒。回执必须匹配原队列内容、源 URL、源 SHA/大小/时长，配方与完整原生记录纳入计划指纹。旧中央导入仅漏了时长（为 0）和 recipe；工具保存旧 artifact JSON，并用同一已验证成片回执补齐投影。不会重新制作素材或改变对象、账号、正文、链接、原队列或日志 ID。

采用两个账本的分阶段审计：先备份两库，将中央原 failed/gen0 加入不可领取的专用 hold；再原生设置 queued/reserved、插入一次性 generation 0→1 授权；最后中央 ready/gen0。应用本身保留 attempt_count=1 和原错误、开始时间，并保留所有历史。第一次正常 claim 才生成真实 generation 1 领取记录，initialize 前消费授权并增加 attempt_count 到 2。没有虚构旧 claim、操作或成功/失败回执。两个中间断点可用同一计划和新鲜节点证据继续，已完成计划重复执行只报告当前状态，不重新激活。

需使用已部署的 179596a 或更新 X native/coordinator（已有精确恢复授权支持）。工具不修改 runtime、服务、并发、其他任务或全局 guard，也不会发送平台请求。生产先生成新 plan，再在 CPU/GPU 分别生成 node-evidence；路径均应在私有维护目录，权限 0600。CLI 只支持这一条，不接受扩大队列范围的参数：

```bash
python -B scripts/recover_x_legacy_initialize_5280.py plan \
  --central /mnt/data-disk/pipeline-dispatch/dispatch.sqlite3 \
  --native /var/lib/x-post-automation/accounts.sqlite3 --output PLAN.json
python -B scripts/recover_x_legacy_initialize_5280.py node-evidence \
  --plan PLAN.json --node-id cpu-main \
  --outbox /mnt/data-disk/pipeline-node-cpu/pipeline-dispatch/receipts.sqlite3 --output CPU.json
# GPU 使用同一 PLAN 和 hk-gpu，outbox=/data/pipeline-dispatch/receipts.sqlite3。
python -B scripts/recover_x_legacy_initialize_5280.py apply \
  --plan PLAN.json --node-evidence CPU.json GPU.json \
  --backup-dir PRIVATE_BACKUPS --actor OPERATOR --confirm-account-unlocked --output RESULT.json
```

gen0 的旧 collector 快照有一个保守行为：若恢复后才递交旧 failed 观察，中央尚未被领取的 ready 可能重新显示 failed。原生授权仍未消费，不产生 Post 或第二轮重试；工具再次调用不会自动重新入队。应用后须核验中央当前状态及原生授权，若发生这种漂移应停留隔离并报告，不能循环执行。正常 generation 1 领取后旧 gen0 观察不能覆盖执行记录。

验证：独立 SQLite 集成测试涵盖只读计划无变更、原历史/成片保留、真正的下一 claim 与 attempt 2、重复 initialize 拒绝、两段中断恢复、任何节点回执拒绝、未知/Post/账号/配方/代次漂移拒绝、计划作用域及新鲜度限制、旧 collector 失败观察不会产生重试循环。生产仅以 stdin 内存执行了只读 snapshot，未创建服务器模块、未 apply。
