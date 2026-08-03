# 回滚方案

## 原则

普通功能回滚只切回已记录的上一 GitHub commit/release，并恢复同 commit 的静态页、app/proxy 与 Nginx 配置。不得用部署前 SQLite backup 覆盖当前数据库，不删除两张新表、direct-test、unknown、run/pool reservation、GPU ledger/manifest、短链或 COS 对象。

## 回滚前冻结事实

1. 停止 runner/prepare runner 新领取并等待当前短事务结束；记录原 service/timer 状态。
2. 记录当前/上一 commit、release 路径、app/static/Nginx SHA、DB 绝对路径/inode/size。
3. 只读导出：auto-config、legacy schedule、direct-test active/unknown/published、pool/queue/run active/unknown/published、GPU manifest/ledger 文件名/hash、已知 TikTok publish IDs。
4. 若有 `publishing/reconciling/unknown`，保留阻断并走既有内部核对；不得改 failed、删除或重发。

## 代码回滚

1. 验证上一 release 对应精确 GitHub commit。
2. 原子切换 CPU current release 到上一 release；恢复同 commit 的 app/proxy/三份静态页/Nginx。
3. 运行 Python 编译与 `nginx -t`，核对静态页 SHA。
4. 按回滚前状态恢复窄 service/timer；先做 health/只读验证，不手工 kick 发布。
5. GPU 未改版：不切 GPU release，不改 profile/env，不清理 ledger。

## SQLite 兼容

- 上一 release 忽略 `tt_post_auto_publish_config` 与 `tt_post_direct_test`；两表保留供前滚继续处理。
- 单例首次保存后，选中账号和共同时间已经同步到 legacy schedule，旧 release 可继续读取。
- 若上一 release 仍不允许同分钟，保持 runner 暂停并升级前滚；不得通过删除账号、错开时间或恢复旧 DB 完成技术回滚。
- 不存在 `tt_post_direct_test_event` 或 `tt_post_auto_due`；同分钟恢复事实位于现有 `tt_post_schedule_run` 与 `tt_post_recurring_pool`。

## direct-test 与现有 recurring run

- direct-test `queued/preparing/ready`：保留任务和 GPU job/manifest；上一 release 不识别时保持暂停，前滚后继续。
- direct-test `publishing/reconciling/unknown`：保留同素材阻断，不自动重试或改终态。
- direct-test `published/failed/canceled`：保留历史，不写回自动池。
- existing recurring run 已 claimed 但未 bind：保留 run 和精确 pool reservation，前滚后由恢复流程继续；不得生成同 slot 替代 run。

## 回滚验证

1. 上一 release、service/timer/Nginx 状态与记录一致。
2. `PRAGMA integrity_check=ok`，DB inode 未被 backup 替换。
3. 回滚前后 config/schedule/direct-test/pool/queue/run/publish ID 集合无删除；GPU ledger/manifest/COS/短链计数不减少。
4. 浏览器只读页面可访问；无配置保存、无真实 Post。

## 不可由代码回滚撤销

- 已在 TikTok 发布的 Post；
- 已初始化但结果 unknown 的请求；
- 已上传 COS 对象、短链 wrapper、GPU manifest/ledger。

这些对象必须走独立、明确授权的核对/清理流程。只有 SQLite 物理损坏且所有写入已停时，才可另行批准灾难恢复；这不属于 027 普通回滚。
