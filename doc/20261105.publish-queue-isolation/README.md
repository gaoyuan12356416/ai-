# 发布队列独立执行

2026-11-05：按本次用户要求取消历史未知结果、同账号其他任务和发布先后顺序对独立队列的阻断。单条失败或未知结果保留在原任务，其他任务继续执行。

## 实现范围

- X：统一调度、自动模板、个人发布、手动计划、转推候选和批处理执行移除跨任务账号历史门禁。
- TikTok：统一调度、自动模板、直接发布和周期任务移除同账号历史及领取顺序门禁。
- Facebook：保留已有素材隔离；没有有效素材标识的历史未知任务也只隔离原任务，不再占用整页。真实运行中的 Page 写入互斥保持。
- YouTube：复核当前已上线的任务级隔离，不覆盖同日上线的原实现。
- 报表文案改为同一素材或任务的结果待核对；保留当前读取性能修复。

身份权限、真实平台临时锁定/限流、同素材防重、原任务未知回执和有效租约保持。新版本只迁移统一调度锁的范围，不重置任务、重发未知结果或修改已有发布事实。旧执行节点所持租约和操作描述仍可完成。

## 验证

运行相关队列、原任务未知结果、同素材防重、FB 素材隔离、旧租约连续性回归；在生产数据库的在线备份上连续初始化新调度器两次，比较任务、领取历史、操作及发布回执全表摘要，必须保持一致。

## 发布与回滚

先提交并推送 GitHub，再从 CPU 服务器只读 Git checkout fetch 并 archive 精确提交。`ops/publish-queue-isolation` 保存针对各现有发布目录的差异，`files.json` 校验修改文件的前后内容，其他并行改动随当前目录完整保留。

```sh
python3.9 scripts/deploy_publish_queue_isolation.py prepare SOURCE COMMIT DEPLOYMENT
python3.9 scripts/deploy_publish_queue_isolation.py activate DEPLOYMENT x tt_auto tt fb coordinator report app
python3.9 scripts/deploy_publish_queue_isolation.py rollback DEPLOYMENT LANE
```

发布脚本在数据盘保存旧文件、在线 SQLite 备份、旧目录、PID、排空记录及迁移演练结果。仅在受影响 HTTP 服务已无处理线程/连接时切换，临时入口限制有自动撤销定时器；无法排空则放弃该服务切换，其他服务可独立继续。执行节点、GPU 和主 API 不重启。报表仅更新源目录，不触发报告发送。

回滚仅恢复本次代码；不得恢复数据库备份覆盖当前账本、回执、凭据或已发生的业务事实。实际提交、部署路径和线上验证记录由本次发布交付记录补充。
