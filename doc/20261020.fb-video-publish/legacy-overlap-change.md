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
