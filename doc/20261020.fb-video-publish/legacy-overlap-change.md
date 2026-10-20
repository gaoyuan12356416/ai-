# 取消外部旧发布队列对新 Page 发布的限制

2026-10-20，用户要求取消 Page 管理中“旧版发布队列仍在使用此Page”的限制。
原实现只要发现旧 Java/MySQL 队列配置使用同一 Page，便在列表显示不可发布，
并在预检、提交和执行前检查中拒绝该 Page；配置本身仍可保存。

新手动模块的 `SourceRepository.list_pages` 不再查询 `legacy_conflicts`。
兼容字段 `legacy_conflict` 固定为 false；Page 的授权状态仍来自有效凭据数量。
该来源适配器同时用于列表、配置、预检、提交和 worker，UI 无须修改。
同 SQLite 的活动/unknown 事务锁及负责人范围继续按原合同执行。
旧自动发布模块自身规则没有变更。

验证：

- 来源/短链 30 项通过：旧队列重叠不禁用 Page；旧队列查询不可用不影响新模块；
  无授权 Page 仍不可用，负责人和产品范围有效，不返回凭据。
- 发布核心 41 项通过，包括 active/unknown 阻断、跨租户 Page 互斥和冻结任务。
- SQL broker 18 项通过。
- 独立只读代码复核确认新模块不存在其他外部旧队列门禁。

部署仅替换 CPU 的以下两个运行文件，使用
`scripts/deploy_fb_video_legacy_overlap.py`：

- `/opt/fb-auto-post/current/features/fb_video_publish/sources.py`
- `/root/drama_material_service/features/fb_video_publish/sources.py`

部署前验证旧源码 SHA256 为
`048e0e7385782f9e663980c29ae2066fc8d01e6e1341e854c07fec1a1c1f227c`。
备份两个文件、在线 SQLite 和服务状态；等待任务排空后，只重启 FB sidecar，
恢复原有 worker/timer 状态。无需数据库迁移、主 API 或 GPU 服务重启。
回滚使用本次 manifest，仅恢复这两个源码文件，不恢复数据库、不删除短链。

## 上线证据

代码提交 `3a1dbb80b322008b2752471692e4672072f20429`，已先推送 GitHub，
CPU 从该提交独立检出至 `/mnt/data-disk/fb-video-publish/releases/3a1dbb8`。
2026-10-20 15:04 北京时间完成切换。

两份运行文件 SHA256 均为
`25ac2f5ba3ecacf9b0791fcaeb2f522f303851dc7b2251d183f41c1901a10c5f`。
所有原 timer/worker 的 active 和 enabled 状态与备份一致；
主 API PID 仍为 `935255`，没有重启。FB/main 健康接口正常，SQLite quick_check=ok。
验证期间手动发布任务数为0，没有发送真实 Facebook 测试帖子。

公网登录刷新后，Page `1003072359564923`（劇情瞬間）和
`1027121640493846`（今晚追劇吧）均显示“授权可用”，旧队列禁用原因消失。
此次操作期间用户新增的 Page 配置正常保留，最新列表共3项。
视频发布页读取同一真实账号配置，3个Page复选框均可操作、默认未选中；
截图中的2个Page不再禁选。验证只读取页面，不保存草稿或提交批次。

部署脚本的隔离模拟覆盖：正常更新两个文件、第二文件复制失败恢复原文件、
恢复服务状态失败后保留新文件及manifest。后者不会再次强停可能已有任务的服务；
进入客户端恢复阶段后，后续故障需按当前事实重新排空再回滚。

准确回滚命令（仅回退本次规则修改）：

```bash
cd /mnt/data-disk/fb-video-publish/releases/3a1dbb8
python3 scripts/deploy_fb_video_legacy_overlap.py --rollback /mnt/data-disk/fb-video-publish/backups/fb-video-legacy-overlap-20261020T070420.194618Z-3a1dbb80/manifest.json
```

该命令保留数据库、任务、配置和短链；发现活动任务或未知文件hash时停止切换。
若要继续回滚整个模块，先完成本次局部回滚，再按原 `deploy.md` 的发布事实保护流程执行。
