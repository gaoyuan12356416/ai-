# 部署与回滚
CPU 43.166.187.96，香港GPU 43.154.250.89。CPU数据/mnt/data-disk/pipeline-dispatch，GPU数据/data/pipeline-dispatch；协调器127.0.0.1:18846，经受保护连接访问，不开放公网。
前置social-publish-isolation-20261102已complete；X当前目录/mnt/data-disk/social-publish-isolation-20261102/x-b4d81b30fda15d33e21b16f426b6cbeeab9b8e10。其他渠道各有独立发布目录，必须先抓非秘密源码摘要和演练补丁。
提交GitHub后拉取确切SHA，先安装不接单协调器/节点/原生桥/监控；原数据库在线备份，增量迁移。
顺序X→FB→TT→YT：冻结cutoff、原任务暂停清单、在途单条及资源锁；停止旧领取，等待正在执行单条和必要后续发布；不得扩大到整批。hold保持素材预留/去重。
旧流程排空并核对回执后才启用新领取。仅用正常到期任务验收。受限清单保存版本、配置、任务、成片SHA和回执，不输出秘密。
制作领取时间与发布时间分别保留：FB 已由原计划器生成的 planned/queued 随机制作任务可立即制作，并遵守原制作重试时间；不扩大原计划器的预建范围。TT 自动任务使用协调器私有 JSON 的显式 `preparation_policy.tt_auto_prepare_ahead_seconds`，本次核对生产原 `TT_AUTO_POST_PREPARE_AHEAD_SECONDS=14400`，两处必须一致；原生桥仍将自身 executor 的配置传给原领取校验。发布阶段不提前，`cutover_due_at` 始终使用原发布时间，因此未来预约不会因提前制作被当成历史积压暂停。X 保持原到期资格。未领取的 generation0 制作任务重新扫描后刷新资格时间；已领取、暂停和待核对任务不重置。
回滚先关闭新领取、排空在途和outbox，再退代码配置；保留当前账本/token/成片/暂停/回执，未知结果继续核对。确切命令在实际切换清单产生后补充。目前尚未本次生产切换。
